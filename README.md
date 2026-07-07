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

## Uninstall

```sh
./uninstall.sh              # remove the skill symlink and the managed clone
./uninstall.sh --keep-clone # remove only the symlink
```

It never deletes a checkout it didn't create (e.g. your own working copy) — it
prints how to remove that yourself. The pairing credential is left in place; the
script prints its path so you can delete it for a full wipe.

Or by hand: `rm ~/.claude/skills/sign-with-trezor` (the symlink) and, if bootstrapped,
`rm -rf ~/.local/share/trezor-skill` (the clone).

## After install

**The device must be paired once before first use** — signing returns
`{"error":"not_paired"}` until then. Check with `scripts/trezor_signer.py status`.
Follow the setup the installer prints: create the venv, then pair
(`scripts/trezor_signer.py pair`). Pairing saves a credential to your user config
directory (`%APPDATA%\trezor-skill\`, `~/Library/Application Support/trezor-skill/`,
or `~/.config/trezor-skill/`) — not the repo. Signing commands and details are in
[`SKILL.md`](SKILL.md).
