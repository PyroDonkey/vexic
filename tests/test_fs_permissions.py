import os
import subprocess
from pathlib import Path

import pytest

from vexic.fs_permissions import (
    _assert_owner_only_listing,
    _icacls_restrict_args,
    ensure_owner_only,
)

ACCOUNT = "RYAN-DESKTOP\\Ryan"
SID = "S-1-5-21-1-2-3-1001"


def _assert_listing(lines: list[str], *, directory: bool = False) -> None:
    _assert_owner_only_listing(
        lines,
        account=ACCOUNT,
        sid=SID,
        directory=directory,
        display_name="secret.json",
    )


def test_listing_verifier_accepts_single_owner_full_control_file() -> None:
    _assert_listing([f"C:\\x\\secret.json {ACCOUNT}:(F)"])


def test_listing_verifier_rejects_files_with_extra_principals() -> None:
    with pytest.raises(PermissionError):
        _assert_listing(
            [
                f"C:\\x\\secret.json {ACCOUNT}:(F)",
                "                  NT AUTHORITY\\SYSTEM:(F)",
            ]
        )


def test_listing_verifier_rejects_missing_current_user_grant() -> None:
    with pytest.raises(PermissionError):
        _assert_listing(["C:\\x\\secret.json OTHERPC\\Mallory:(F)"])


def test_listing_verifier_rejects_user_entry_without_full_control() -> None:
    with pytest.raises(PermissionError):
        _assert_listing([f"C:\\x\\secret.json {ACCOUNT}:(R)"])


def test_listing_verifier_rejects_inherited_entries() -> None:
    with pytest.raises(PermissionError):
        _assert_listing([f"C:\\x\\secret.json {ACCOUNT}:(I)(F)"])


def test_listing_verifier_accepts_hardened_directory_dacl() -> None:
    # The CPython 3.13 mode-0o700 directory DACL: owner + privileged
    # principals that bypass per-file DACLs anyway.
    _assert_listing(
        [
            f"C:\\x\\artifacts {ACCOUNT}:(OI)(CI)(F)",
            "               NT AUTHORITY\\SYSTEM:(OI)(CI)(F)",
            "               BUILTIN\\Administrators:(OI)(CI)(F)",
            "               OWNER RIGHTS:(OI)(CI)(F)",
        ],
        directory=True,
    )


def test_listing_verifier_matches_user_by_sid_when_name_unresolved() -> None:
    _assert_listing([f"C:\\x\\secret.json *{SID}:(F)"])


def test_listing_verifier_rejects_directory_without_inheriting_user_grant() -> None:
    with pytest.raises(PermissionError):
        _assert_listing(
            [f"C:\\x\\artifacts {ACCOUNT}:(F)"],
            directory=True,
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
    username = os.environ.get("USERNAME", "")
    assert username and username.lower() in ace_lines[0].lower(), ace_lines


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
