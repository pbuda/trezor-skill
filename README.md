# trezor-skill

A Claude skill for signing blockchain transactions with a **Trezor Safe 7** hardware
wallet: EIP-1559 Ethereum transactions (sign-only) and GalaChain EIP-712 transfers
(optional submit). Device I/O prefers Bluetooth LE and falls back to USB.

See [`SKILL.md`](SKILL.md) for how the skill works and how to run the signing flows.

## Install

`install.sh` clones (or updates) this repo and symlinks it into your global Claude
skills directory, so Claude discovers it by its manifest name (`sign-with-trezor`):

```sh
./install.sh
```

On a machine with nothing cloned yet:

```sh
curl -fsSL https://raw.githubusercontent.com/pbuda/trezor-skill/main/install.sh | bash
```

The script is idempotent — re-running updates the clone and refreshes the link. It
finishes by printing the one-time device setup (venv + pairing) for your platform.

### Overrides

| Variable | Default | Purpose |
| --- | --- | --- |
| `TREZOR_SKILL_SRC` | `~/.local/share/trezor-skill` | Where the clone lives (when bootstrapping). |
| `CLAUDE_SKILLS_DIR` | `~/.claude/skills` | Global Claude skills directory to link into. |

## After install

Follow the one-time setup the installer prints — create the venv, then pair the
device (`scripts/trezor_signer.py pair`), which writes a gitignored
`trezor_credential.json`. Details and the signing commands are in [`SKILL.md`](SKILL.md).
