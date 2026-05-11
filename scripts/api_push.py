"""Push the latest local commit to GitHub via `gh api` (Git Database API).

Use this when plain `git push` is blocked at the network level but `gh` works.

Workflow:
    1. Read remote HEAD on `main` via `gh api`.
    2. Diff local HEAD vs remote: collect adds/modifies and deletions.
    3. Upload blobs via /git/blobs for new/changed files (using gh api with JSON body).
    4. Build a tree with /git/trees (base_tree = remote tree).
    5. Create commit with /git/commits (message taken from local HEAD).
    6. Fast-forward /git/refs/heads/main to the new commit.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "Nangongyeee/redpaper"
BRANCH = "main"
LOCAL_REPO = Path(__file__).resolve().parents[1]
GH_BIN = os.environ.get("GH_BIN") or str(Path.home() / ".local" / "bin" / "gh")
if not Path(GH_BIN).exists():
    GH_BIN = "gh"


def gh_api(method: str, path: str, body: dict | None = None) -> dict:
    cmd = [GH_BIN, "api", "-X", method, path, "--input", "-"]
    stdin_data: str | None = None
    if body is not None:
        stdin_data = json.dumps(body)
    else:
        # gh requires --input even for GETs; remove if no body
        cmd = [GH_BIN, "api", "-X", method, path]
    r = subprocess.run(
        cmd,
        input=stdin_data,
        capture_output=True,
        text=True,
        cwd=LOCAL_REPO,
    )
    if r.returncode != 0:
        # Include the full response body on failure for debugging
        raise RuntimeError(
            f"gh api {method} {path} failed:\n"
            f"  exit={r.returncode}\n"
            f"  stderr={r.stderr.strip()}\n"
            f"  stdout={r.stdout.strip()[:1000]}"
        )
    return json.loads(r.stdout) if r.stdout.strip() else {}


def run(cmd: list[str]) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=LOCAL_REPO)
    if res.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {res.stderr}")
    return res.stdout


def file_mode(path: Path) -> str:
    if not path.exists():
        return "100644"
    try:
        st = path.stat()
    except OSError:
        return "100644"
    return "100755" if st.st_mode & 0o111 else "100644"


def upload_blob(path: Path) -> str:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
        body = {"content": text, "encoding": "utf-8"}
    except UnicodeDecodeError:
        body = {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"}
    res = gh_api("POST", f"/repos/{REPO}/git/blobs", body)
    return res["sha"]


def diff_against(remote_sha: str) -> tuple[list[str], list[str]]:
    out = run(["git", "diff-tree", "-r", "--name-status", remote_sha, "HEAD"])
    added: list[str] = []
    deleted: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        path = parts[-1]
        if status.startswith("D"):
            deleted.append(path)
        else:
            added.append(path)
    return added, deleted


def main() -> None:
    # 1) Remote head
    ref = gh_api("GET", f"/repos/{REPO}/git/refs/heads/{BRANCH}")
    remote_sha = ref["object"]["sha"]
    commit = gh_api("GET", f"/repos/{REPO}/git/commits/{remote_sha}")
    remote_tree_sha = commit["tree"]["sha"]
    print(f"remote head:  {remote_sha}")
    print(f"remote tree:  {remote_tree_sha}")

    # 2) Diff
    added, deleted = diff_against(remote_sha)
    print(f"files to add/modify: {len(added)}, delete: {len(deleted)}")

    # 3) Upload blobs
    tree_items: list[dict] = []
    for i, rel in enumerate(added, 1):
        p = LOCAL_REPO / rel
        if not p.exists():
            print(f"  [skip] {rel}: missing on disk")
            continue
        sha = upload_blob(p)
        tree_items.append({
            "path": rel,
            "mode": file_mode(p),
            "type": "blob",
            "sha": sha,
        })
        if i % 5 == 0 or i == len(added):
            print(f"  uploaded {i}/{len(added)}: {rel[:60]}")

    for rel in deleted:
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": None})

    if not tree_items:
        print("nothing to push.")
        return

    # 4) Tree
    tree = gh_api(
        "POST",
        f"/repos/{REPO}/git/trees",
        {"base_tree": remote_tree_sha, "tree": tree_items},
    )
    new_tree_sha = tree["sha"]
    print(f"new tree:     {new_tree_sha}")

    # 5) Commit
    msg = run(["git", "log", "-1", "--pretty=%B"]).rstrip()
    new_commit = gh_api(
        "POST",
        f"/repos/{REPO}/git/commits",
        {"message": msg, "tree": new_tree_sha, "parents": [remote_sha]},
    )
    new_commit_sha = new_commit["sha"]
    print(f"new commit:   {new_commit_sha}")

    # 6) Fast-forward ref
    gh_api(
        "PATCH",
        f"/repos/{REPO}/git/refs/heads/{BRANCH}",
        {"sha": new_commit_sha, "force": False},
    )
    print("pushed.")
    print("To align local main with remote: `git fetch && git reset --hard origin/main`")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
