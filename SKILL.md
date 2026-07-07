---
name: sign-with-trezor
description: Sign blockchain transactions and messages with a Trezor Safe 7 hardware wallet, with optional submission. Use when the user wants to hardware-sign or submit an on-chain operation with a Trezor.
---

# Sign with a Trezor Safe 7

Two signing flows share one device backend:

1. **EVM transaction** (`sign_evm.py`) — build + sign an EIP-1559 tx, **sign-only, never broadcasts**.
2. **GalaChain EIP-712** (`galachain_transfer.py`) — sign a GalaChain DTO as typed data and optionally **submit** it to the ops API.

Both are pure Python (no `@gala-chain/api`, no Node). All device I/O goes through
`trezor_signer.py`, which prefers **Bluetooth Low Energy** and falls back to USB.

## Architecture

The Safe 7 (`T3W1`) speaks THP (protocol v2) over either BLE or its composite
WebUSB+FIDO-HID USB interface. Transport selection lives in one place
(`trezor_signer.py`); the orchestrators just shell out and read JSON back.

```
orchestrator (sign_evm.py / galachain_transfer.py)
        │  subprocess + JSON
        ▼
trezor_signer.py  ──►  BLE (bleak / CoreBluetooth on macOS) — preferred
                  ──►  USB (WebUSB via libusb)              — fallback
                                │
                                ▼
                          Trezor Safe 7
```

`trezor_signer.py` subcommands: `status`, `pair`, `address`, `sign` (EIP-1559),
`sign-typed` (EIP-712). The Safe 7 speaks THP with **only CodeEntry pairing**, so
the device must be paired once before any signing. Pairing writes a credential to
your **user config directory** — `%APPDATA%\trezor-skill\` on Windows,
`~/Library/Application Support/trezor-skill/` on macOS, `~/.config/trezor-skill/`
on Linux — **not** the skill folder, and every later sign replays it
non-interactively. The credential is transport-agnostic (same file for BLE and
USB). `status` reports whether a credential exists; until it does, signing
commands return `{"error":"not_paired"}`. The only per-sign human step is
confirming on the device screen (host app name `bridgekeeper-plugin`).

### Runtime layout

* **macOS / native Linux / native Windows** — one venv runs everything; the
  orchestrator invokes `trezor_signer.py` in-process (`sys.executable`).
* **WSL** — the Safe 7 can't be forwarded into WSL via `usbipd`, so the
  orchestrator shells out to Windows-side `python.exe` for the signer (the
  signer still picks BLE or USB on the Windows host).

## Pairing (required before first use)
The device must be paired once; signing returns `{"error":"not_paired"}` until it
is. Check anytime with `scripts/trezor_signer.py status`. Run the steps below from
the skill directory; the code lives under `scripts/`.

### macOS / native Linux / native Windows
1. Single venv: `python -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt`
2. **macOS only** — grant Bluetooth permission to your terminal in
   System Settings → Privacy & Security → Bluetooth.
3. **Pair the device** (interactive — shows a code on the Trezor to type back):
   ```
   .venv/bin/python scripts/trezor_signer.py pair
   ```
   The printed address must match the wallet. The credential is saved to your user
   config directory (see above), not the skill folder.

### WSL (legacy split)
1. **Windows Python** with trezorlib: `python.exe -m pip install --user -r scripts/requirements-windows.txt`
2. **WSL venv**: `python -m venv .venv && .venv/bin/pip install -r scripts/requirements-wsl.txt`
3. **Pair the device** (from Windows):
   ```
   python.exe scripts/trezor_signer.py pair
   ```

### Transport selection
By default the signer tries BLE first then falls back to USB. Force a transport
with `--transport ble|usb|auto` on `trezor_signer.py`, `sign_evm.py`, or
`galachain_transfer.py`.

> **Auto-lock gotcha:** if the Safe 7 idles and locks, the stored credential is rejected
> (`{"error":"not_paired"}`) for `address`/`sign`/`sign-typed`. This is **not** a real
> depairing — just **unlock the device** and retry; the same credential works again. Only
> re-run `pair` if it still fails after unlocking.

---

## Flow 1 — EVM transaction (sign-only)
```
.venv/bin/python scripts/sign_evm.py \
    --rpc-url <read-only RPC> --to 0xRecipient --value-eth 0.01 \
    [--data 0x...] [--path "m/44'/60'/0'/0/0"] [--transport auto|ble|usb] \
    [--nonce N] [--gas-limit N] [--max-fee-per-gas WEI] [--max-priority-fee-per-gas WEI] \
    [--chain-id N] [--yes] [--json]
```
- Sender (`from`) always comes from the device; nonce/gas/fees auto-fetched (any overridable).
- Output: `raw_tx`, `tx_hash`, `from`, `to`, `nonce`, `chain_id`. **Never broadcasts** — submit
  `raw_tx` yourself with `eth_sendRawTransaction`.
- Sender verified twice (device + recovered-from-signature) before output.

---

## Flow 2 — GalaChain TransferToken (EIP-712)
```
.venv/bin/python scripts/galachain_transfer.py \
    --to "eth|<checksummed-address>" --quantity 1 \
    [--collection GALA --category Unit --token-type none --additional-key none --instance 0] \
    [--path "m/44'/60'/0'/0/0"] [--expected-from 0x...] [--transport auto|ble|usb] \
    [--submit --base-url <ops-api> --channel asset --contract token-contract --method TransferToken] \
    [--identity "eth|..."] [--yes] [--json]
```
Without `--submit` it prints the signed body and stops. With `--submit` it POSTs to
`<base-url>/api/<channel>/<contract>/<method>` and reports the chain `Status` (1 = ok).

### Why EIP-712 (not GalaChain's standard scheme)
GalaChain's default signing is a **raw** secp256k1 over `keccak256(canonical payload)`. A
Trezor refuses to blind-sign an arbitrary 32-byte digest, so the standard scheme is
impossible on hardware. EIP-712 typed data is the structured path the device supports; the
chaincode recovers the signer's identity (`eth|<address>`) from the signature.

### The mechanics (validated byte-for-byte against `@gala-chain/api`, accepted on prod)
- **Domain** `{name:"GalaConnect"}`, **primaryType** `GalaTransaction`; types generated from
  the DTO shape (`string`/`int256`/`bool`/nested-struct/`T[]`). `quantity` is a **string**.
- **`prefix`** (`\x19Ethereum Signed Message:\n<len>`, computed to a fixpoint) is in the
  **message but not the types** — so it's excluded from the EIP-712 hash; still carried in
  the body.
- **Signature** = `0x` + `r‖s‖v`, `v` ∈ {`1b`,`1c`}, low-S.
- **Identity** = `eth|<checksummed-address-without-0x>`. `from` defaults to the recovered
  signer; the recipient `eth|<addr>` may resolve to a registered `client|<alias>`.
- **Headers**: send `x-identity-lookup-key: eth|<addr>`; **drop** `x-user-encryption-key`
  (it's `sha256(privateKey)` — impossible for a HW wallet, and the next-gen ops API verifies
  from the signature alone).

`galachain_eip712.py` is the standalone EIP-712 library (build/verify/selftest CLI); it has
**no GalaChain dependency** and reproduces the chaincode's digest exactly.

## Safety properties
- **EVM flow never broadcasts.** GalaChain flow submits only with explicit `--submit`.
- **Recover-sender gate**: every signature is verified to recover the device address before
  output/submission.
- **On-device gate**: the Trezor displays and must approve the exact tx / typed data.

## Limitations / next steps
- GalaChain: only `TransferToken` is wired; other DTOs work via the same `galachain_eip712`
  build if their shape is supplied.
- EVM: EIP-1559 only; empty access list; contract calls show generic data without token
  **definitions** (not yet wired).
- The `@gala-chain/api` parity was confirmed against a local install; the Python port is now
  the source of truth and carries no runtime dependency on it.
