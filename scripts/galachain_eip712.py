#!/usr/bin/env python3
"""Standalone GalaChain EIP-712 signing helpers — pure Python, no @gala-chain/api.

Reproduces, in Python, exactly what the chaincode verifies (validated byte-for-byte
against @gala-chain/api):

  * generate_types          — dynamic EIP-712 types from the DTO shape
                              (string->string, int->int256, bool->bool, dict->struct, list->T[])
  * calculate_personal_sign_prefix
                              — GalaChain's recursive "\\x19Ethereum Signed Message:\\n<len>"
                                over the deterministic JSON payload (fixpoint)
  * eip712_digest           — keccak256(0x1901 + domainSeparator + hashStruct(message)),
                                the 32-byte hash the Trezor signs and the chain recovers from
  * recover_address         — secp256k1 public-key recovery -> EIP-55 address

The `prefix` is placed in the message but NOT in the types — so it is excluded from the
EIP-712 hash (repo-faithful; the chaincode accepts this). It is still carried in the
submitted body.

Runs in the WSL venv (eth-account / eth-keys / eth-utils). Device signing is delegated
to win_trezor_signer.py (Windows). CLI mirrors the old Node oracle: build|verify|selftest.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

from eth_account.messages import encode_typed_data, _hash_eip191_message
from eth_keys import keys
from eth_utils import to_checksum_address

DOMAIN = {"name": "GalaConnect"}
PRIMARY_TYPE = "GalaTransaction"
PERSONAL_SIGN_HEADER = "Ethereum Signed Message:\n"
# getPayloadToSign strips these signing-related fields before serializing.
_STRIPPED = ("signature", "multisig", "trace")


# --------------------------------------------------------------------------- #
# Dynamic EIP-712 type generation (port of generateEIP712Types)
# --------------------------------------------------------------------------- #
def generate_types(type_name: str, params: dict) -> dict:
    types: dict = {type_name: []}

    def add_field(name, value, parent, only_get_type=False):
        if isinstance(value, list):
            t = add_field(name, value[0], parent, True)
            if not only_get_type:
                types[parent].append({"name": name, "type": (t or name) + "[]"})
        elif isinstance(value, dict):
            if name not in types:
                types[name] = []
            for k, v in value.items():
                add_field(k, v, name)
            if not only_get_type:
                types[parent].append({"name": name, "type": name})
        else:
            if isinstance(value, bool):       # bool before int (bool is an int subclass)
                eip_type = "bool"
            elif isinstance(value, str):
                eip_type = "string"
            elif isinstance(value, int):
                eip_type = "int256"
            else:
                raise ValueError(f"Unsupported type {type(value).__name__} for {name}={value!r}")
            if only_get_type:
                return eip_type
            if not any(t["name"] == name for t in types[parent]):
                types[parent].append({"name": name, "type": eip_type})
        return None

    for key, value in params.items():
        add_field(key, value, type_name)
    return types


# --------------------------------------------------------------------------- #
# Deterministic JSON + personal-sign prefix (port of serialize + calculatePersonalSignPrefix)
# --------------------------------------------------------------------------- #
def deterministic_json(obj: dict) -> str:
    """json-stringify-deterministic equivalent: recursive key sort, compact, JS escaping."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _payload_to_sign_json(obj: dict) -> str:
    plain = {k: v for k, v in obj.items() if k not in _STRIPPED}
    return deterministic_json(plain)


def calculate_personal_sign_prefix(payload: dict) -> str:
    payload_length = len(_payload_to_sign_json(payload))
    prefix = PERSONAL_SIGN_HEADER + str(payload_length)
    new_payload = {**payload, "prefix": prefix}
    new_length = len(_payload_to_sign_json(new_payload))
    if payload_length == new_length:
        return prefix
    return calculate_personal_sign_prefix(new_payload)


# --------------------------------------------------------------------------- #
# EIP-712 digest + recovery
# --------------------------------------------------------------------------- #
def eip712_digest(domain: dict, types: dict, message: dict) -> bytes:
    # Only declared-type fields are hashed; pass a message limited to them so any
    # extra carrier fields (e.g. prefix) cannot leak in. EIP712Domain is derived
    # from `domain` by eth_account, so strip it from the struct types we hand over.
    struct_types = {k: v for k, v in types.items() if k != "EIP712Domain"}
    declared = {f["name"] for f in struct_types[PRIMARY_TYPE]}
    msg = {k: v for k, v in message.items() if k in declared}
    signable = encode_typed_data(
        domain_data=domain, message_types=struct_types, message_data=msg
    )
    return _hash_eip191_message(signable)


def recover_address(digest: bytes, signature_hex: str) -> str:
    sig = signature_hex[2:] if signature_hex.startswith("0x") else signature_hex
    r = int(sig[0:64], 16)
    s = int(sig[64:128], 16)
    v = int(sig[128:130], 16)
    rec_id = v - 27 if v >= 27 else v
    signature = keys.Signature(vrs=(rec_id, r, s))
    pub = signature.recover_public_key_from_msg_hash(digest)
    return pub.to_checksum_address()


# --------------------------------------------------------------------------- #
# Build the full artifact set for a base DTO
# --------------------------------------------------------------------------- #
def build(base: dict) -> dict:
    unique_key = base.get("uniqueKey") or f"trezor-{uuid.uuid4()}"
    message = {k: v for k, v in base.items() if k != "uniqueKey"}
    message["uniqueKey"] = unique_key                       # uniqueKey appended last

    gt_types = generate_types(PRIMARY_TYPE, message)        # no EIP712Domain
    types_for_trezor = {"EIP712Domain": [{"name": "name", "type": "string"}], **gt_types}
    prefix = calculate_personal_sign_prefix(message)

    typed_data = {
        "domain": DOMAIN,
        "primaryType": PRIMARY_TYPE,
        "types": types_for_trezor,
        "message": {**message, "prefix": prefix},
    }
    submit_body = {**message, "prefix": prefix, "domain": DOMAIN, "types": gt_types}
    digest = eip712_digest(DOMAIN, gt_types, message)

    return {
        "uniqueKey": unique_key,
        "prefix": prefix,
        "typedData": typed_data,
        "submitBody": submit_body,
        "digest": digest.hex(),
    }


# --------------------------------------------------------------------------- #
# CLI (mirrors the retired Node oracle: build | verify | selftest)
# --------------------------------------------------------------------------- #
def _read_stdin() -> dict:
    raw = sys.stdin.read().strip()
    return json.loads(raw) if raw else {}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "build":
        print(json.dumps(build(_read_stdin()), ensure_ascii=False))
    elif mode == "verify":
        data = _read_stdin()
        digest = bytes.fromhex(data["digest"])
        print(json.dumps({"recovered": recover_address(digest, data["signature"])}))
    elif mode == "selftest":
        built = build(_read_stdin())
        digest = bytes.fromhex(built["digest"])
        priv = keys.PrivateKey(os.urandom(32))
        sig = priv.sign_msg_hash(digest)                    # secp256k1 over the digest
        sig_hex = f"{sig.r:064x}{sig.s:064x}{sig.v + 27:02x}"
        recovered = recover_address(digest, sig_hex)
        expected = priv.public_key.to_checksum_address()
        ok = recovered.lower() == expected.lower()
        print(json.dumps({"ok": ok, "recovered": recovered, "expected": expected, "digest": built["digest"]}))
        return 0 if ok else 1
    else:
        sys.stderr.write("usage: galachain_eip712.py build|verify|selftest\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
