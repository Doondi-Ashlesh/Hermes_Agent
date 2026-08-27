#!/usr/bin/env python3
"""Verify every relative markdown link and heading anchor across the docs resolves.

Cross-references breaking silently is the failure mode this guards (CLAUDE.md §2).
Anchor rules follow GitHub's: lowercase, drop punctuation, one hyphen per space
character — runs of whitespace are NOT collapsed.
"""

from __future__ import annotations

import pathlib
import re
import sys

SKIP_PREFIXES = ("http://", "https://", "mailto:", "#!")


def slug(heading: str) -> str:
    text = heading.strip()
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)   # [label](url) -> label
    text = re.sub(r"[`*~]|<[^>]+>", "", text)              # code, emphasis, html
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)                   # punctuation, incl. ·
    return re.sub(r"\s", "-", text)                        # one hyphen per space


def anchors(path: pathlib.Path) -> set[str]:
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(.*)", line)
        if match:
            found.add(slug(match.group(1)))
    return found


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    files = sorted([*root.glob("*.md"), *root.glob("docs/**/*.md")])
    problems: list[str] = []

    for path in files:
        text = path.read_text(encoding="utf-8")
        for _, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", text):
            if target.startswith(SKIP_PREFIXES):
                continue
            file_part, _, anchor = target.partition("#")
            resolved = (path.parent / file_part).resolve() if file_part else path.resolve()
            rel = path.relative_to(root)
            if not resolved.exists():
                problems.append(f"{rel}: missing file -> {target}")
            elif anchor and resolved.suffix == ".md" and anchor not in anchors(resolved):
                problems.append(f"{rel}: missing anchor -> {target}")

    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"checked {len(files)} markdown files, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
