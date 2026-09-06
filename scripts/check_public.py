#!/usr/bin/env python3
"""Check the public tree for private session artifacts and obvious credentials.

This is a release guard, not a replacement for reviewing content before publish.
Only tracked and non-ignored new files are checked when Git is available.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git", ".omx", "dist", "__pycache__", ".venv"}
FORBIDDEN_PATHS = {".omx", "context", "reviews", "handoff", "history"}
PRIVATE_SUFFIXES = {".ansi", ".db", ".jsonl", ".log", ".sqlite"}
PATTERNS = (
    ("personal home path", re.compile(r"/(?:Users|home)/(?!(?:example|demo|runner|test)(?:/|\b))[A-Za-z0-9_.-]+/")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("API credential", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{24,}\b")),
    ("AWS access key ID", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "AWS secret access key assignment",
        re.compile(r"\bAWS_SECRET_ACCESS_KEY\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])"),
    ),
)


def public_files(root: Path) -> list[Path]:
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, check=True, capture_output=True,
        )
        return sorted({root / name.decode() for name in result.stdout.split(b"\0") if name})
    return sorted(path for path in root.rglob("*")
                  if path.is_file() and not EXCLUDED.intersection(path.relative_to(root).parts))


def inspect(root: Path) -> list[str]:
    findings = []
    for path in public_files(root):
        relative = path.relative_to(root)
        if path.is_symlink():
            findings.append(f"{relative}: symlinks are not part of public releases")
            continue
        credential_name = path.name == ".env" or path.name.startswith(".env.")
        if credential_name:
            findings.append(f"{relative}: credential file name")
        if (FORBIDDEN_PATHS.intersection(relative.parts)
                or path.suffix in PRIVATE_SUFFIXES
                or path.name == "lineage.json"):
            findings.append(f"{relative}: private/runtime artifact path")
        if not path.is_file():
            findings.append(f"{relative}: missing or non-regular file")
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError:
            findings.append(f"{relative}: unexpected binary file; review before publishing")
            continue
        for label, pattern in PATTERNS:
            if pattern.search(content):
                # Never echo a suspected credential into CI output.
                findings.append(f"{relative}: possible {label}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    findings = inspect(args.root.resolve())
    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("Public-tree check passed (paths, text artifacts, credential patterns).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
