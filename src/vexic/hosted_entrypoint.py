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
    # Guarded one-time repair: skip the O(volume) walk only when a previous
    # run of THIS code finished it for the same uid:gid -- proven by the
    # completion sentinel written after the walk plus a correctly-owned root.
    # Root ownership alone is not proof: the pre-sentinel entrypoint chowned
    # the root first, so an interrupted old repair leaves a correct root over
    # mis-owned children; the sentinel forces one unconditional heal on the
    # first boot after this ships. The walk chowns bottom-up, stamps the
    # sentinel, and flips the root last, so an interrupted repair leaves the
    # guard unsatisfied and the next boot resumes it. Assumed out of scope
    # operationally: an external writer (docker exec as root, snapshot
    # restore) planting mis-owned files under a correct root and matching
    # sentinel -- delete the sentinel to force a full re-repair.
    sentinel = root / ".chown-complete"
    stamp = f"{uid}:{gid}"
    try:
        sentinel_matches = sentinel.read_text(encoding="utf-8").strip() == stamp
    except OSError:
        sentinel_matches = False
    if sentinel_matches:
        stat = root.lstat()
        if stat.st_uid == uid and stat.st_gid == gid:
            return
    for current, dir_names, file_names in os.walk(root, topdown=False):
        for name in (*dir_names, *file_names):
            os.lchown(Path(current) / name, uid, gid)
    sentinel.write_text(stamp, encoding="utf-8")
    os.lchown(sentinel, uid, gid)
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
