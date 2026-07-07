#!/usr/bin/env python3
"""Build + sign an EIP-1559 Ethereum transaction with a Trezor Safe 7 — sign only.

Fetches nonce/gas/fees from a read-only RPC, delegates the actual signing to
trezor_signer.py, assembles the signed raw transaction locally, verifies the recovered
sender matches the device, and prints the raw tx. It NEVER broadcasts — you submit the
raw tx yourself.

Runtime layout:
  * macOS / native Linux / native Windows: the signer runs in the same Python venv
    (sys.executable), reaching the Safe 7 over BLE by default and USB as fallback.
  * WSL: the orchestrator runs in the WSL venv, but the Safe 7 can't be forwarded into
    WSL via usbipd, so the signer is shelled out to the Windows-side python.exe over
    the WSL interop bridge. The signer still picks BLE/USB on the Windows host.

Example:
  python sign_evm.py --rpc-url https://ethereum-rpc.publicnode.com \
      --to 0xRecipient... --value-eth 0.01 --yes
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys

import rlp
from eth_account import Account
from eth_utils import keccak, to_canonical_address
from web3 import Web3

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNER_SCRIPT = "trezor_signer.py"  # resolved relative to PROJECT_DIR
DEFAULT_PATH = "m/44'/60'/0'/0/0"
# Fee headroom: maxFee = baseFee * multiplier + priorityFee
BASE_FEE_MULTIPLIER = 2


def _signer_python() -> str:
    """WSL must hop to Windows Python (Safe 7 not forwarded); everywhere else, in-process."""
    if platform.system() == "Linux" and "microsoft" in platform.uname().release.lower():
        return "python.exe"
    return sys.executable


# --------------------------------------------------------------------------- #
# Signer delegation
# --------------------------------------------------------------------------- #
def _call_signer(subcommand: str, *, stdin: str | None = None,
                 extra: list[str] | None = None, credential: str | None = None,
                 transport: str | None = None) -> dict:
    """Invoke trezor_signer.py and return its parsed JSON result."""
    cmd = [_signer_python(), SIGNER_SCRIPT]
    if credential:
        cmd += ["--credential", credential]
    if transport:
        cmd += ["--transport", transport]
    cmd += [subcommand, *(extra or [])]

    proc = subprocess.run(
        cmd, input=stdin, capture_output=True, text=True, cwd=PROJECT_DIR
    )
    # Surface device prompts / diagnostics (stderr) to the user in real terms.
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr.replace("\r", ""))

    stdout = proc.stdout.replace("\r", "").strip()
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError(
            f"Signer produced no output (exit {proc.returncode}). "
            "Is the Trezor connected and unlocked?"
        )
    result = json.loads(lines[-1])
    if "error" in result:
        raise RuntimeError(f"Signer error [{result['error']}]: {result.get('message', '')}")
    return result


def device_address(path: str, credential: str | None, transport: str | None) -> str:
    return _call_signer(
        "address", extra=["--path", path], credential=credential, transport=transport,
    )["address"]


# --------------------------------------------------------------------------- #
# Transaction building (read-only RPC)
# --------------------------------------------------------------------------- #
def _normalize_data(data: str | None) -> bytes:
    data = (data or "").lower()
    if data.startswith("0x"):
        data = data[2:]
    return bytes.fromhex(data)


def build_tx(w3: Web3, sender: str, args) -> dict:
    chain_id = args.chain_id if args.chain_id is not None else w3.eth.chain_id

    to = Web3.to_checksum_address(args.to) if args.to else None
    value = args.value if args.value is not None else w3.to_wei(args.value_eth or 0, "ether")
    data = _normalize_data(args.data)

    nonce = args.nonce if args.nonce is not None else w3.eth.get_transaction_count(sender, "pending")

    if args.max_priority_fee_per_gas is not None:
        max_priority = args.max_priority_fee_per_gas
    else:
        max_priority = w3.eth.max_priority_fee
    if args.max_fee_per_gas is not None:
        max_fee = args.max_fee_per_gas
    else:
        base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
        max_fee = base_fee * BASE_FEE_MULTIPLIER + max_priority

    if args.gas_limit is not None:
        gas_limit = args.gas_limit
    else:
        gas_limit = w3.eth.estimate_gas(
            {"from": sender, "to": to, "value": value, "data": data}
        )

    return {
        "path": args.path,
        "chain_id": chain_id,
        "nonce": nonce,
        "to": to,
        "value": value,
        "data": data,
        "gas_limit": gas_limit,
        "max_fee_per_gas": max_fee,
        "max_priority_fee_per_gas": max_priority,
        "expected_from": sender,
    }


# --------------------------------------------------------------------------- #
# Signed raw tx assembly
# --------------------------------------------------------------------------- #
def assemble_signed_tx(tx: dict, y_parity: int, r: int, s: int) -> tuple[str, str]:
    to = to_canonical_address(tx["to"]) if tx["to"] else b""
    fields = [
        tx["chain_id"], tx["nonce"], tx["max_priority_fee_per_gas"], tx["max_fee_per_gas"],
        tx["gas_limit"], to, tx["value"], tx["data"], [],  # empty accessList
        y_parity, r, s,
    ]
    raw = b"\x02" + rlp.encode(fields)
    return "0x" + raw.hex(), "0x" + keccak(raw).hex()


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #
def _describe(tx: dict, w3: Web3) -> str:
    gwei = lambda wei: f"{w3.from_wei(wei, 'gwei')} gwei"
    max_cost = tx["gas_limit"] * tx["max_fee_per_gas"]
    return "\n".join([
        "Transaction to sign (EIP-1559, sign-only):",
        f"  from (device) : {tx['expected_from']}",
        f"  to            : {tx['to']}",
        f"  value         : {w3.from_wei(tx['value'], 'ether')} ETH ({tx['value']} wei)",
        f"  data          : 0x{tx['data'].hex()}" + (" (empty)" if not tx["data"] else ""),
        f"  nonce         : {tx['nonce']}",
        f"  chainId       : {tx['chain_id']}",
        f"  gas limit     : {tx['gas_limit']}",
        f"  maxFeePerGas  : {gwei(tx['max_fee_per_gas'])}",
        f"  maxPriorityFee: {gwei(tx['max_priority_fee_per_gas'])}",
        f"  max gas cost  : {w3.from_wei(max_cost, 'ether')} ETH",
    ])


def main() -> int:
    p = argparse.ArgumentParser(description="Sign an EIP-1559 tx with a Trezor Safe 7 (sign-only).")
    p.add_argument("--rpc-url", required=True, help="Read-only RPC endpoint (never broadcast).")
    p.add_argument("--to", required=True, help="Recipient / contract address.")
    p.add_argument("--path", default=DEFAULT_PATH, help="BIP-44 derivation path.")
    p.add_argument("--credential", default=None, help="THP credential JSON (default: alongside signer).")
    p.add_argument("--transport", choices=("auto", "ble", "usb"), default="auto",
                   help="Transport preference passed to the signer.")

    value_group = p.add_mutually_exclusive_group()
    value_group.add_argument("--value", type=int, help="Value in wei.")
    value_group.add_argument("--value-eth", type=float, help="Value in ETH.")

    p.add_argument("--data", default="", help="Calldata hex (for contract calls / tokens).")
    p.add_argument("--chain-id", type=int, default=None, help="Override chainId (default: from RPC).")
    p.add_argument("--nonce", type=int, default=None, help="Override nonce.")
    p.add_argument("--gas-limit", type=int, default=None, help="Override gas limit.")
    p.add_argument("--max-fee-per-gas", type=int, default=None, help="Override maxFeePerGas (wei).")
    p.add_argument("--max-priority-fee-per-gas", type=int, default=None, help="Override priority fee (wei).")
    p.add_argument("--yes", action="store_true", help="Skip the host-side confirmation prompt.")
    p.add_argument("--json", action="store_true", help="Emit only the JSON result on stdout.")
    args = p.parse_args()

    w3 = Web3(Web3.HTTPProvider(args.rpc_url))
    if not w3.is_connected():
        print(f"ERROR: cannot reach RPC at {args.rpc_url}", file=sys.stderr)
        return 2

    # 1. Sender comes from the device — the only source of truth for 'from'.
    sender = device_address(args.path, args.credential, args.transport)

    # 2. Build the tx from read-only chain state.
    tx = build_tx(w3, sender, args)

    # 3. Show it and gate on confirmation (host side; the device will gate again).
    if not args.json:
        print(_describe(tx, w3), file=sys.stderr)
    if not args.yes:
        try:
            if input("\nProceed to sign on the Trezor? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Aborted.", file=sys.stderr)
                return 1
        except EOFError:
            print("No TTY for confirmation; re-run with --yes to proceed.", file=sys.stderr)
            return 1

    # 4. Delegate signing to the device (hex-encode bytes for the JSON hop).
    tx_for_signer = {**tx, "data": tx["data"].hex()}
    sig = _call_signer(
        "sign", stdin=json.dumps(tx_for_signer),
        credential=args.credential, transport=args.transport,
    )

    # 5. Assemble + verify (tamper check: recovered sender must equal the device address).
    raw_tx, tx_hash = assemble_signed_tx(tx, sig["v"], int(sig["r"], 16), int(sig["s"], 16))
    recovered = Account.recover_transaction(raw_tx)
    if recovered.lower() != sender.lower():
        print(f"ERROR: signature sender {recovered} != device {sender}. Refusing.", file=sys.stderr)
        return 5

    result = {
        "raw_tx": raw_tx,
        "tx_hash": tx_hash,
        "from": sender,
        "to": tx["to"],
        "nonce": tx["nonce"],
        "chain_id": tx["chain_id"],
    }
    if args.json:
        print(json.dumps(result))
    else:
        print("\nSigned (NOT broadcast). Submit raw_tx with eth_sendRawTransaction:\n", file=sys.stderr)
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
