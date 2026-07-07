"""Shared helpers for the Trezor orchestrators.

Locates the Python that can reach the device (Windows Python under WSL, otherwise
in-process) and — the important bit — **forces one-time pairing** before any
signing. If the device isn't paired, `ensure_paired()` drives the interactive
pairing flow with the terminal attached, rather than letting a signing command
dead-end on `{"error": "not_paired"}`.
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys

SIGNER_SCRIPT = "trezor_signer.py"


def signer_python() -> str:
    """WSL must hop to Windows Python (the Safe 7 isn't forwarded into WSL);
    everywhere else the signer runs in-process."""
    if platform.system() == "Linux" and "microsoft" in platform.uname().release.lower():
        return "python.exe"
    return sys.executable


def _cmd(subcommand: str, *, credential: str | None, transport: str | None,
         extra: list[str] | None = None) -> list[str]:
    cmd = [signer_python(), SIGNER_SCRIPT]
    if credential:
        cmd += ["--credential", credential]
    if transport:
        cmd += ["--transport", transport]
    return cmd + [subcommand, *(extra or [])]


def is_paired(*, credential: str | None, transport: str | None, cwd: str) -> bool:
    """Ask the signer whether a stored credential exists. No device I/O."""
    proc = subprocess.run(
        _cmd("status", credential=credential, transport=transport),
        capture_output=True, text=True, cwd=cwd,
    )
    lines = [ln for ln in proc.stdout.replace("\r", "").splitlines() if ln.strip()]
    if not lines:
        return False
    return bool(json.loads(lines[-1]).get("paired"))


def ensure_paired(*, credential: str | None, transport: str | None, cwd: str) -> None:
    """Guarantee the device is paired before signing.

    If it isn't, run the interactive pairing flow (terminal attached so the user
    can type the code shown on the Trezor), then confirm it stuck. Raises with a
    clear directive if pairing can't complete (e.g. no interactive terminal).
    """
    if is_paired(credential=credential, transport=transport, cwd=cwd):
        return

    sys.stderr.write(
        "\nTrezor is not paired yet — starting one-time pairing.\n"
        "Watch the device: enter the code it shows when prompted below.\n\n"
    )
    # Inherit stdio so the pairing-code prompt is interactive.
    rc = subprocess.run(
        _cmd("pair", credential=credential, transport=transport), cwd=cwd
    ).returncode

    if rc != 0 or not is_paired(credential=credential, transport=transport, cwd=cwd):
        raise RuntimeError(
            "Pairing did not complete. Run it directly in a terminal, then retry:\n"
            f"    {signer_python()} {SIGNER_SCRIPT} pair"
        )
    sys.stderr.write("\nPairing complete — continuing.\n\n")
