#!/usr/bin/env python3
"""Pure-Python release audit: decompress git objects + difflib. No git binary."""
from __future__ import annotations

import difflib
import zlib
from pathlib import Path

ROOT = Path("/Users/ryan/GitHub/vexic")
GIT = ROOT / ".git"
OUT = ROOT / "tmp_audit_out.txt"
PKG = Path("/Users/ryan/.local/share/uv/tools/vexic/lib/python3.13/site-packages")


def read_ref(name: str) -> str:
    p = GIT / name
    if p.is_file():
        return p.read_text().strip()
    for line in (GIT / "packed-refs").read_text().splitlines():
        if line.startswith("#") or " " not in line:
            continue
        sha, ref = line.split(" ", 1)
        if ref == name:
            return sha
    raise SystemExit(f"missing ref {name}")


def load_object(sha: str) -> tuple[str, bytes]:
    raw = zlib.decompress((GIT / "objects" / sha[:2] / sha[2:]).read_bytes())
    nul = raw.index(b"\x00")
    kind, _ = raw[:nul].decode().split(" ", 1)
    return kind, raw[nul + 1 :]


def parse_commit(data: bytes) -> dict:
    text = data.decode()
    headers, _, message = text.partition("\n\n")
    out: dict = {"message": message, "parents": []}
    for line in headers.splitlines():
        if line.startswith("tree "):
            out["tree"] = line[5:]
        elif line.startswith("parent "):
            out["parents"].append(line[7:])
    return out


def parse_tree(data: bytes) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    i = 0
    while i < len(data):
        nul = data.index(b"\x00", i)
        mode, name = data[i:nul].decode().split(" ", 1)
        sha = data[nul + 1 : nul + 21].hex()
        entries[name] = (mode, sha)
        i = nul + 21
    return entries


def walk_tree(tree_sha: str, prefix: str = "") -> dict[str, str]:
    _, data = load_object(tree_sha)
    files: dict[str, str] = {}
    for name, (mode, sha) in parse_tree(data).items():
        path = f"{prefix}/{name}" if prefix else name
        if mode.startswith("40"):
            files.update(walk_tree(sha, path))
        else:
            files[path] = sha
    return files


def ancestors(tip: str) -> list[str]:
    chain: list[str] = []
    seen: set[str] = set()
    cur = tip
    while cur and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        parents = parse_commit(load_object(cur)[1])["parents"]
        # BFS all parents for reachability set; keep first-parent for order later
        cur = parents[0] if parents else ""
    return chain


def all_reachable(tip: str) -> set[str]:
    seen: set[str] = set()
    stack = [tip]
    while stack:
        sha = stack.pop()
        if sha in seen:
            continue
        seen.add(sha)
        parents = parse_commit(load_object(sha)[1])["parents"]
        stack.extend(parents)
    return seen


def oneline(sha: str) -> str:
    msg = parse_commit(load_object(sha)[1])["message"].splitlines()[0]
    return f"{sha[:7]} {msg}"


def blob(sha: str | None) -> bytes | None:
    if not sha:
        return None
    return load_object(sha)[1]


def udiff(path: str, a: bytes | None, b: bytes | None) -> str:
    al = [] if a is None else a.decode("utf-8", "replace").splitlines(keepends=True)
    bl = [] if b is None else b.decode("utf-8", "replace").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            al,
            bl,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def main() -> None:
    origin_main = read_ref("refs/remotes/origin/main")
    origin_dev = read_ref("refs/remotes/origin/dev")
    head = (GIT / "HEAD").read_text().strip()
    head_sha = read_ref(head[5:]) if head.startswith("ref: ") else head

    parts: list[str] = []
    parts.append("=== git rev-parse origin/main origin/dev HEAD ===")
    parts.append(origin_main)
    parts.append(origin_dev)
    parts.append(head_sha)

    main_set = all_reachable(origin_main)
    # newest-first: walk first-parent from tip until main
    only: list[str] = []
    cur = origin_dev
    while cur and cur not in main_set:
        only.append(cur)
        parents = parse_commit(load_object(cur)[1])["parents"]
        cur = parents[0] if parents else ""
    # Also include non-first-parent commits reachable from tip not in main
    # Match `git log A..B` = reachable from B, not from A
    all_dev = all_reachable(origin_dev) - main_set
    # Order: topological approx by walking and collecting
    ordered = []
    seen = set()
    stack = [origin_dev]
    while stack:
        sha = stack.pop()
        if sha in seen or sha in main_set:
            continue
        seen.add(sha)
        ordered.append(sha)
        parents = parse_commit(load_object(sha)[1])["parents"]
        # push parents in reverse so first parent processed first-ish
        for p in reversed(parents):
            stack.append(p)

    parts.append("\n=== git log --oneline origin/main..dev ===")
    for sha in ordered:
        parts.append(oneline(sha))

    main_tree = parse_commit(load_object(origin_main)[1])["tree"]
    dev_tree = parse_commit(load_object(origin_dev)[1])["tree"]
    main_files = walk_tree(main_tree)
    dev_files = walk_tree(dev_tree)
    changed = sorted(p for p in set(main_files) | set(dev_files) if main_files.get(p) != dev_files.get(p))

    parts.append("\n=== git diff --stat origin/main...dev ===")
    total_ins = total_del = 0
    rows = []
    for path in changed:
        a = blob(main_files.get(path))
        b = blob(dev_files.get(path))
        al = [] if a is None else a.decode("utf-8", "replace").splitlines()
        bl = [] if b is None else b.decode("utf-8", "replace").splitlines()
        sm = difflib.SequenceMatcher(None, al, bl)
        ins = dels = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "insert":
                ins += j2 - j1
            elif tag == "delete":
                dels += i2 - i1
            elif tag == "replace":
                dels += i2 - i1
                ins += j2 - j1
        rows.append((path, ins, dels))
        total_ins += ins
        total_del += dels
    width = max((len(p) for p, _, _ in rows), default=0)
    for path, ins, dels in rows:
        parts.append(f" {path.ljust(width)} | {ins + dels:4d} {'+' * min(ins, 40)}{'-' * min(dels, 40)}")
    parts.append(
        f" {len(changed)} files changed, {total_ins} insertions(+), {total_del} deletions(-)"
    )

    parts.append("\n=== git diff --name-only origin/main...dev ===")
    parts.extend(changed)

    focus = [
        "src/vexic/mcp_http.py",
        "src/vexic/recorders/cli.py",
        "src/vexic/recorders/hosted_ingest.py",
        "src/vexic/recorders/hosted_prime.py",
        "src/vexic/recorders/status.py",
        "src/vexic/__init__.py",
        "pyproject.toml",
        "README.md",
        "tests/test_packaging_surface.py",
    ]
    parts.append("\n=== FULL DIFF (focus paths) ===")
    for path in focus:
        d = udiff(path, blob(main_files.get(path)), blob(dev_files.get(path)))
        if d:
            parts.append(f"diff --git a/{path} b/{path}")
            parts.append(d.rstrip("\n"))
        else:
            parts.append(f"# unchanged or missing: {path}")

    parts.append("\n=== git diff --name-only origin/main...dev -- tests/** ===")
    test_files = [p for p in changed if p.startswith("tests/")]
    parts.extend(test_files)

    parts.append("\n=== FULL DIFF tests/** and docs/** ===")
    for path in [p for p in changed if p.startswith("tests/") or p.startswith("docs/")]:
        d = udiff(path, blob(main_files.get(path)), blob(dev_files.get(path)))
        parts.append(f"diff --git a/{path} b/{path}")
        parts.append(d.rstrip("\n") if d else "# empty")

    text = "\n".join(parts) + "\n"
    OUT.write_text(text)
    print(f"wrote {OUT} ({len(text)} bytes, {len(changed)} files)")


if __name__ == "__main__":
    main()
