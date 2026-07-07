#!/usr/bin/env python3
"""Sign a GalaChain TransferToken with a Trezor (EIP-712) and optionally submit it.

Standalone — pure Python, no @gala-chain/api, no Node. On macOS / native Linux / native
Windows the signer runs in the same venv (BLE-first, USB fallback). In WSL the signer
is shelled out to Windows-side python.exe (the Safe 7 can't be forwarded into WSL).
Flow:

    build typed data (galachain_eip712)  ──►  Trezor sign_typed_data (Windows)
                                                      │
        recover-sender gate (must equal device)  ◄────┘
                                                      │
        POST /api/<channel>/<contract>/<method>  ◄────┘   (only with --submit)

Verification, identity, and the signed body are assembled here; the device only turns
the typed data into a signature. Without --submit it prints the signed body and stops.

Example (sign only):
  .venv/bin/python scripts/galachain_transfer.py --to "eth|09A8AC..." --quantity 1
Example (sign + submit to the ops API):
  .venv/bin/python scripts/galachain_transfer.py --to "eth|09A8AC..." --quantity 1 \
      --submit --base-url https://operation-api-next-production.prod.internal.us-east-va-1.galachain.com
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import galachain_eip712 as gc
from signer_common import SIGNER_SCRIPT, ensure_paired, signer_python

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = "m/44'/60'/0'/0/0"
DEFAULT_BASE_URL = "https://operation-api-next-production.prod.internal.us-east-va-1.galachain.com"


def _sign_typed(typed_data: dict, path: str, expected_from: str | None,
                transport: str) -> dict:
    """Delegate EIP-712 signing to trezor_signer.py."""
    cmd = [signer_python(), SIGNER_SCRIPT, "--transport", transport,
           "sign-typed", "--path", path]
    if expected_from:
        cmd += ["--expected-from", expected_from]
    proc = subprocess.run(cmd, input=json.dumps(typed_data), capture_output=True, text=True, cwd=HERE)
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr.replace("\r", ""))
    lines = [ln for ln in proc.stdout.replace("\r", "").splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError(
            f"Signer produced no output (exit {proc.returncode}). "
            "Is the Trezor connected and unlocked?"
        )
    res = json.loads(lines[-1])
    if "error" in res:
        raise RuntimeError(f"Signer error [{res['error']}]: {res.get('message', '')}")
    return res


def _submit(base_url: str, channel: str, contract: str, method: str,
            body: dict, identity: str) -> tuple[int, dict]:
    url = f"{base_url}/api/{channel}/{contract}/{method}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={
            "Content-Type": "application/json",
            "accept": "application/json",
            "x-identity-lookup-key": identity,  # x-user-encryption-key dropped: impossible for HW
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:  # GalaChain returns JSON error bodies on 4xx
        return e.code, json.loads(e.read().decode())


def main() -> int:
    p = argparse.ArgumentParser(description="Sign (and optionally submit) a GalaChain TransferToken with a Trezor.")
    p.add_argument("--to", required=True, help="Recipient identity, e.g. eth|<checksummed-address>.")
    p.add_argument("--quantity", required=True, help="Amount to transfer (string/integer).")
    p.add_argument("--collection", default="GALA")
    p.add_argument("--category", default="Unit")
    p.add_argument("--token-type", dest="token_type", default="none")
    p.add_argument("--additional-key", dest="additional_key", default="none")
    p.add_argument("--instance", default="0")
    p.add_argument("--path", default=DEFAULT_PATH, help="BIP-44 derivation path.")
    p.add_argument("--expected-from", default=None, help="Assert the device address.")
    p.add_argument("--transport", choices=("auto", "ble", "usb"), default="auto",
                   help="Transport preference passed to the signer.")
    p.add_argument("--submit", action="store_true", help="POST the signed DTO to the ops API.")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--channel", default="asset")
    p.add_argument("--contract", default="token-contract")
    p.add_argument("--method", default="TransferToken")
    p.add_argument("--identity", default=None, help="x-identity-lookup-key (default: eth|<device>).")
    p.add_argument("--yes", action="store_true", help="Skip the host-side confirmation before submit.")
    p.add_argument("--json", action="store_true", help="Emit only the JSON result on stdout.")
    args = p.parse_args()

    # 1. Build the EIP-712 typed data (pure Python; matches @gala-chain/api).
    base = {
        "to": args.to,
        "tokenInstance": {
            "collection": args.collection,
            "category": args.category,
            "type": args.token_type,
            "additionalKey": args.additional_key,
            "instance": args.instance,
        },
        "quantity": str(args.quantity),
    }
    built = gc.build(base)
    if not args.json:
        print("\n".join([
            "GalaChain TransferToken (EIP-712):",
            f"  to        : {args.to}",
            f"  quantity  : {args.quantity} {args.collection}",
            f"  token     : {args.collection}/{args.category}/{args.token_type}/{args.additional_key}/{args.instance}",
            f"  uniqueKey : {built['uniqueKey']}",
            f"  digest    : 0x{built['digest']}",
        ]), file=sys.stderr)

    # 2. Sign on the device (pairing is required first — force it if needed).
    ensure_paired(credential=None, transport=args.transport, cwd=HERE)
    res = _sign_typed(built["typedData"], args.path, args.expected_from, args.transport)
    device, signature = res["address"], res["signature"]

    # 3. Gate: the recovered signer must equal the device address (tamper check).
    recovered = gc.recover_address(bytes.fromhex(built["digest"]), signature)
    if recovered.lower() != device.lower():
        print(f"ERROR: recovered {recovered} != device {device}. Refusing.", file=sys.stderr)
        return 5

    body = {**built["submitBody"], "signature": "0x" + signature}
    # GalaChain identity is eth|<checksummed-address-without-0x>.
    eth_identity = f"eth|{device[2:] if device.startswith('0x') else device}"
    identity = args.identity or eth_identity

    # 4. Submit, or stop with the signed body.
    if not args.submit:
        result = {"from": eth_identity, "body": body}
        print(json.dumps(result) if args.json else json.dumps(result, indent=2))
        if not args.json:
            print("\nNot submitted. Re-run with --submit to POST.", file=sys.stderr)
        return 0

    if not args.yes:
        try:
            prompt = f"\nSubmit transfer of {args.quantity} {args.collection} to {args.to}? [y/N] "
            if input(prompt).strip().lower() not in ("y", "yes"):
                print("Aborted.", file=sys.stderr)
                return 1
        except EOFError:
            print("No TTY for confirmation; re-run with --yes to submit.", file=sys.stderr)
            return 1

    status, resp = _submit(args.base_url, args.channel, args.contract, args.method, body, identity)
    chain_status = resp.get("Status")
    result = {"http": status, "status": chain_status, "from": eth_identity, "response": resp}
    print(json.dumps(result) if args.json else json.dumps(result, indent=2))
    return 0 if chain_status == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
