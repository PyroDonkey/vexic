from __future__ import annotations

import os
from pathlib import Path


def main() -> None:
    uid, gid = _runtime_ids(os.environ.get("VEXIC_RUNTIME_USER", "vexic"))
    root = Path(os.environ.get("VEXIC_HOSTED_ROOT", "/data/vexic"))
    if os.getuid() == 0:
        _chown_tree(root, uid, gid)
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    os.execv("/app/.venv/bin/python", _uvicorn_command())


def _runtime_ids(user: str) -> tuple[int, int]:
    import pwd

    entry = pwd.getpwnam(user)
    return entry.pw_uid, entry.pw_gid


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    # Guarded one-time repair: skip the O(volume) walk when the root already
    # belongs to the runtime user. The walk chowns bottom-up and flips the
    # root last, so a correctly-owned root proves every child was repaired;
    # an interrupted repair leaves the root mismatched and the next boot
    # resumes the walk instead of trusting a half-repaired tree.
    stat = root.lstat()
    if stat.st_uid == uid and stat.st_gid == gid:
        return
    for current, dir_names, file_names in os.walk(root, topdown=False):
        for name in (*dir_names, *file_names):
            os.lchown(Path(current) / name, uid, gid)
    os.lchown(root, uid, gid)


def _uvicorn_command() -> list[str]:
    return [
        "/app/.venv/bin/python",
        "-m",
        "uvicorn",
        "vexic.hosted_control_plane_http:create_app",
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        os.environ.get("PORT", "8000"),
    ]


if __name__ == "__main__":
    main()
