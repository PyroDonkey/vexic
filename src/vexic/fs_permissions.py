import os
import stat
import subprocess
from pathlib import Path

# Owner-only enforcement for secret-bearing files and artifact directories.
# Dependency-free leaf (stdlib only): recorders and the local service enforce
# filesystem confidentiality without host wiring. POSIX uses mode bits; NT
# strips ACL inheritance and grants the current user SID only, because chmod
# mode bits are cosmetic on Windows.


def _current_user_sid() -> str:
    completed = subprocess.run(
        ["whoami", "/user", "/fo", "csv"],
        capture_output=True,
        text=True,
        check=True,
    )
    last_row = completed.stdout.strip().splitlines()[-1]
    sid = last_row.rsplit(",", 1)[-1].strip().strip('"')
    if not sid.upper().startswith("S-1-"):
        raise PermissionError("could not resolve the current user SID")
    return sid


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


def _ensure_owner_only_nt(path: Path, *, directory: bool) -> None:
    args = _icacls_restrict_args(path, _current_user_sid(), directory=directory)
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
    inherited = [line for line in ace_lines if "(I)" in line]
    if inherited:
        raise PermissionError(
            f"{path.name} still carries inherited access control entries"
        )
    if not ace_lines or len(ace_lines) > _NT_PRIVILEGED_ACE_BUDGET:
        raise PermissionError(
            f"{path.name} carries {len(ace_lines)} access control entries; "
            f"expected 1-{_NT_PRIVILEGED_ACE_BUDGET} non-inherited entries"
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
