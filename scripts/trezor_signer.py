#!/usr/bin/env python3
"""Trezor Safe 7 (T3W1) signer — runs in any Python with native device access.

Transport preference is BLE-first, then USB. On macOS the device is reached over BLE
(via `bleak` / CoreBluetooth) from the same Python that runs the orchestrator. On the
WSL setup the orchestrator shells out to a Windows-side Python that runs this same
script (USB falls back automatically if BLE is unavailable on that host).

Subcommands:
  pair        Interactive one-time THP CodeEntry pairing -> saves a credential file.
  address     Print the Ethereum address at a derivation path (uses stored credential).
  sign        Read an EIP-1559 tx request (JSON on stdin) -> print {address, v, r, s}.
  sign-typed  Sign EIP-712 typed data (JSON on stdin) -> print {address, signature}.

Protocol notes (trezorlib 0.20.x):
  * The Safe 7 speaks THP (protocol v2). trezorlib's get_client() runs a legacy
    protocol-v1 probe that mis-handles THP-only devices, so we build TrezorClientThp
    directly and skip the probe.
  * The device offers only the CodeEntry pairing method (no SkipPairing), so a session
    cannot be opened until paired. We pair once, persist the StaticCredential, and
    replay it on every subsequent connection. The credential is transport-agnostic:
    the same trezor_credential.json works for BLE and USB.
  * A cold device occasionally returns a zero-length packet on the first THP read; we
    retry connection a few times.

stdout carries ONLY the final JSON result. All human/diagnostic text goes to stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from trezorlib.transport import get_transport
from trezorlib.client import AppManifest
from trezorlib.thp.client import TrezorClientThp
from trezorlib.thp.credentials import StaticCredential
from trezorlib.thp.pairing import default_pairing_flow
from trezorlib import ethereum
from trezorlib.tools import parse_path

APP_NAME = "bridgekeeper-plugin"
DEFAULT_PATH = "m/44'/60'/0'/0/0"
CONNECT_RETRIES = 5
RETRY_DELAY_S = 0.4
DEFAULT_CRED = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "trezor_credential.json"
)
TRANSPORT_CHOICES = ("auto", "ble", "usb")


def _log(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _emit(obj: dict) -> None:
    """Write the single machine-readable result line to stdout."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _load_credential(path: str) -> StaticCredential | None:
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    return StaticCredential(
        trezor_pubkey=bytes.fromhex(d["trezor_pubkey"]),
        host_privkey=bytes.fromhex(d["host_privkey"]),
        credential=bytes.fromhex(d["credential"]),
    )


def _save_credential(path: str, cred: StaticCredential) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "trezor_pubkey": cred.trezor_pubkey.hex(),
                "host_privkey": cred.host_privkey.hex(),
                "credential": cred.credential.hex(),
            },
            fh,
        )


def _open_transport(mode: str):
    """Pick a transport. 'auto' tries BLE first then USB; 'ble'/'usb' force one."""
    if mode == "usb":
        return get_transport(prefix_search=True)
    if mode == "ble":
        return get_transport("ble:", prefix_search=True)
    try:
        return get_transport("ble:", prefix_search=True)
    except Exception as err:
        _log(f"[transport] BLE unavailable ({err!r}); falling back to USB")
        return get_transport(prefix_search=True)


def _connect(transport_mode: str, credentials=(), button_callback=None) -> TrezorClientThp:
    """Build a THP client directly (bypassing the broken v1 probe), with cold retry."""
    last_err: Exception | None = None
    for attempt in range(CONNECT_RETRIES):
        try:
            transport = _open_transport(transport_mode)
            app = AppManifest(
                app_name=APP_NAME,
                credentials=tuple(credentials),
                button_callback=button_callback,
            )
            return TrezorClientThp(
                app=app, transport=transport, mapping=None, model=None
            )
        except Exception as err:  # cold-channel ZLP -> TransportException/ProtocolError
            last_err = err
            _log(f"[connect] attempt {attempt + 1}/{CONNECT_RETRIES} failed: {err}")
            time.sleep(RETRY_DELAY_S)
    assert last_err is not None
    raise last_err


def _hex_to_bytes(value: str) -> bytes:
    value = (value or "").lower()
    if value.startswith("0x"):
        value = value[2:]
    return bytes.fromhex(value)


def cmd_pair(args) -> int:
    def code_entry() -> str:
        _log("")
        _log(">>> A pairing code is shown on the Trezor screen.")
        sys.stderr.write(">>> Type that code here and press Enter: ")
        sys.stderr.flush()
        return input().strip()

    client = _connect(args.transport)
    if client.pairing.is_paired():
        _emit({"status": "already_paired"})
        return 0

    cred = default_pairing_flow(
        client.pairing, code_entry_callback=code_entry, request_credential=True
    )
    _save_credential(args.credential, cred)
    session = client.get_session()
    addr = ethereum.get_address(session, parse_path(args.path))
    _emit({"status": "paired", "credential_file": args.credential, "address": addr})
    return 0


def cmd_address(args) -> int:
    cred = _load_credential(args.credential)
    if cred is None:
        _emit({"error": "not_paired", "message": "Run `pair` first."})
        return 3
    client = _connect(args.transport, credentials=[cred])
    if not client.pairing.is_paired():
        _emit({"error": "not_paired", "message": "Stored credential rejected; re-pair."})
        return 3
    session = client.get_session()
    addr = ethereum.get_address(session, parse_path(args.path))
    _emit({"address": addr})
    return 0


def cmd_sign(args) -> int:
    req = json.load(sys.stdin)
    cred = _load_credential(args.credential)
    if cred is None:
        _emit({"error": "not_paired", "message": "Run `pair` first."})
        return 3

    def button_callback(br) -> None:
        _log(f"[device] confirm on Trezor: {getattr(br, 'code', br)}")

    client = _connect(args.transport, credentials=[cred], button_callback=button_callback)
    if not client.pairing.is_paired():
        _emit({"error": "not_paired", "message": "Stored credential rejected; re-pair."})
        return 3

    session = client.get_session()
    path = parse_path(req["path"])
    sender = ethereum.get_address(session, path)

    expected = req.get("expected_from")
    if expected and sender.lower() != expected.lower():
        _emit({"error": "sender_mismatch", "device": sender, "expected": expected})
        return 4

    _log("[device] Review and confirm the transaction on the Trezor. Waiting...")
    v, r, s = ethereum.sign_tx_eip1559(
        session,
        path,
        nonce=int(req["nonce"]),
        gas_limit=int(req["gas_limit"]),
        to=req["to"],
        value=int(req["value"]),
        data=_hex_to_bytes(req.get("data", "")),
        chain_id=int(req["chain_id"]),
        max_gas_fee=int(req["max_fee_per_gas"]),
        max_priority_fee=int(req["max_priority_fee_per_gas"]),
    )
    _emit({"address": sender, "v": v, "r": r.hex(), "s": s.hex()})
    return 0


def cmd_sign_typed(args) -> int:
    """Sign EIP-712 typed data (JSON on stdin) -> {address, signature(130-hex, v=1b/1c)}."""
    data = json.load(sys.stdin)
    cred = _load_credential(args.credential)
    if cred is None:
        _emit({"error": "not_paired", "message": "Run `pair` first."})
        return 3

    def button_callback(br) -> None:
        _log(f"[device] confirm on Trezor: {getattr(br, 'code', br)}")

    client = _connect(args.transport, credentials=[cred], button_callback=button_callback)
    if not client.pairing.is_paired():
        _emit({"error": "not_paired", "message": "Stored credential rejected; re-pair."})
        return 3

    session = client.get_session()
    path = parse_path(args.path)
    sender = ethereum.get_address(session, path)
    if args.expected_from and sender.lower() != args.expected_from.lower():
        _emit({"error": "sender_mismatch", "device": sender, "expected": args.expected_from})
        return 4

    _log("[device] Review and confirm the typed data on the Trezor. Waiting...")
    ret = ethereum.sign_typed_data(session, path, data, metamask_v4_compat=True)
    sig = bytearray(ret.signature)
    if sig[64] in (0, 1):  # normalize recovery id to Ethereum's 27/28 (-> hex 1b/1c)
        sig[64] += 27
    _emit({"address": ret.address, "signature": bytes(sig).hex()})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Trezor Safe 7 signer.")
    parser.add_argument("--credential", default=DEFAULT_CRED,
                        help="Path to the THP credential JSON file.")
    parser.add_argument("--transport", choices=TRANSPORT_CHOICES, default="auto",
                        help="Transport preference: auto (BLE then USB), ble, or usb.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_pair = sub.add_parser("pair", help="Interactive one-time pairing.")
    p_pair.add_argument("--path", default=DEFAULT_PATH)
    p_pair.set_defaults(func=cmd_pair)

    p_addr = sub.add_parser("address", help="Print ETH address at a path.")
    p_addr.add_argument("--path", default=DEFAULT_PATH)
    p_addr.set_defaults(func=cmd_address)

    p_sign = sub.add_parser("sign", help="Sign an EIP-1559 tx (JSON on stdin).")
    p_sign.set_defaults(func=cmd_sign)

    p_typed = sub.add_parser("sign-typed", help="Sign EIP-712 typed data (JSON on stdin).")
    p_typed.add_argument("--path", default=DEFAULT_PATH)
    p_typed.add_argument("--expected-from", default=None, help="Assert device address matches.")
    p_typed.set_defaults(func=cmd_sign_typed)

    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _log("Aborted.")
        return 130
    except Exception as err:  # surface as JSON so the orchestrator can react
        _emit({"error": type(err).__name__, "message": str(err)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
