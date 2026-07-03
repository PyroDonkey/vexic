import os
import subprocess
from pathlib import Path

import pytest

from vexic.fs_permissions import (
    _icacls_restrict_args,
    ensure_owner_only,
)


def test_icacls_restrict_args_reference_sid_not_account_name() -> None:
    args = _icacls_restrict_args(Path("C:/x/secret.json"), "S-1-5-21-1-2-3-1001")
    assert args[0] == "icacls"
    assert "/inheritance:r" in args
    grant = args[args.index("/grant:r") + 1]
    # SID form is locale- and rename-proof; icacls needs the * prefix.
    assert grant == "*S-1-5-21-1-2-3-1001:F"


def _ace_lines(path: Path) -> list[str]:
    listing = subprocess.run(
        ["icacls", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in listing.splitlines() if ":(" in line]


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL enforcement")
def test_ensure_owner_only_leaves_a_single_owner_ace_on_files(tmp_path: Path) -> None:
    secret = tmp_path / "recorder.json"
    secret.write_text("{}", encoding="utf-8")

    ensure_owner_only(secret)

    ace_lines = _ace_lines(secret)
    assert len(ace_lines) == 1, ace_lines
    assert "(F)" in ace_lines[0], ace_lines


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL enforcement")
def test_ensure_owner_only_strips_inheritance_on_directories(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    # mode=0o700 matters: CPython 3.13 writes a hardened explicit DACL for it
    # on Windows (owner + privileged principals), which ensure_owner_only
    # tolerates because those principals bypass DACLs anyway.
    artifact_dir.mkdir(mode=0o700)

    ensure_owner_only(artifact_dir, directory=True)

    ace_lines = _ace_lines(artifact_dir)
    assert ace_lines, "expected at least the owner grant"
    assert all("(I)" not in line for line in ace_lines), ace_lines
    assert len(ace_lines) <= 4, ace_lines
    assert any("(OI)(CI)" in line and "(F)" in line for line in ace_lines), ace_lines


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode-bit enforcement")
def test_ensure_owner_only_requires_0600_on_posix(tmp_path: Path) -> None:
    secret = tmp_path / "recorder.json"
    secret.write_text("{}", encoding="utf-8")
    secret.chmod(0o644)

    with pytest.raises(PermissionError):
        ensure_owner_only(secret)

    secret.chmod(0o600)
    ensure_owner_only(secret)
