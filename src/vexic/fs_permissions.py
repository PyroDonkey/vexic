import os
import stat
import subprocess
from pathlib import Path

# Owner-only enforcement for secret-bearing files and artifact directories.
# Dependency-free leaf (stdlib only): recorders and the local service enforce
# filesystem confidentiality without host wiring. POSIX uses mode bits; NT
# strips ACL inheritance and grants the current user SID only, because chmod
# mode bits are cosmetic on Windows.


def _current_user_identity() -> tuple[str, str]:
    """Return ``(account_name, sid)`` for the current Windows user."""
    completed = subprocess.run(
        ["whoami", "/user", "/fo", "csv"],
        capture_output=True,
        text=True,
        check=True,
    )
    last_row = completed.stdout.strip().splitlines()[-1]
    account, _, sid = last_row.rpartition(",")
    account = account.strip().strip('"')
    sid = sid.strip().strip('"')
    if not account or not sid.upper().startswith("S-1-"):
        raise PermissionError("could not resolve the current user account and SID")
    return account, sid


def _icacls_restrict_args(path: Path, sid: str, *, directory: bool = False) -> list[str]:
    # (OI)(CI) makes a directory grant inherit to the files created inside it.
    rights = "(OI)(CI)F" if directory else "F"
    return [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"*{sid}:{rights}",
    ]


# CPython 3.13 hardens os.mkdir(mode=0o700)/mkdtemp on Windows by writing an
# explicit DACL for the owner plus these privileged principals, which hold
# blanket filesystem access regardless of per-file grants. Their presence adds
# no reachable exposure, so verification tolerates up to this many entries.
_NT_PRIVILEGED_ACE_BUDGET = 4


def _assert_owner_only_listing(
    ace_lines: list[str],
    *,
    account: str,
    sid: str,
    directory: bool,
    display_name: str,
) -> None:
    """Fail closed unless an icacls listing shows an owner-only DACL.

    Files must carry exactly one ACE: the current user with full control.
    Directories tolerate the privileged principals CPython 3.13 writes for
    mode-0o700 directories (they bypass per-file DACLs anyway) but still
    require the current user's inheriting full-control grant.
    """
    inherited = [line for line in ace_lines if "(I)" in line]
    if inherited:
        raise PermissionError(
            f"{display_name} still carries inherited access control entries"
        )
    budget = _NT_PRIVILEGED_ACE_BUDGET if directory else 1
    if not ace_lines or len(ace_lines) > budget:
        raise PermissionError(
            f"{display_name} carries {len(ace_lines)} access control entries; "
            f"expected 1-{budget} non-inherited entries"
        )
    # icacls prints the resolved account name, or the raw SID when the name
    # cannot be resolved; match either, case-insensitively for the name.
    user_lines = [
        line
        for line in ace_lines
        if account.lower() in line.lower() or sid in line
    ]
    required_marks = ("(OI)", "(CI)", "(F)") if directory else ("(F)",)
    if not any(
        all(mark in line for mark in required_marks) for line in user_lines
    ):
        raise PermissionError(
            f"{display_name} does not grant the current user "
            f"{'an inheriting ' if directory else ''}full-control entry"
        )


def _ensure_owner_only_nt(path: Path, *, directory: bool) -> None:
    account, sid = _current_user_identity()
    args = _icacls_restrict_args(path, sid, directory=directory)
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode != 0:
        raise PermissionError(
            f"could not restrict {path.name} to the current user (icacls failed)"
        )
    listing = subprocess.run(
        ["icacls", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    ace_lines = [line for line in listing.splitlines() if ":(" in line]
    _assert_owner_only_listing(
        ace_lines,
        account=account,
        sid=sid,
        directory=directory,
        display_name=path.name,
    )


def ensure_owner_only(path: Path, *, directory: bool = False) -> None:
    """Fail closed unless ``path`` is readable by the owning user only.

    POSIX verifies 0o600 (0o700 for directories); callers set the mode at
    create time. NT actively rewrites the DACL: inheritance removed, a single
    full-control entry for the current user, then verified.
    """
    if os.name == "nt":
        _ensure_owner_only_nt(path, directory=directory)
        return
    expected = 0o700 if directory else 0o600
    if stat.S_IMODE(path.stat().st_mode) != expected:
        raise PermissionError(
            f"{path.name} must have owner-only permissions ({oct(expected)})"
        )
