#!/usr/bin/env python3
"""
check-citations.py — stdlib-only citation path validator

Finds repo paths cited in docs/, README.md, plan.md and source comments
(kernel/src/, mathd/src/), and fails when any cited path does not exist.

Usage:
    python tools/check-citations.py

Output:
    - Prints summary to stdout
    - Writes full report to docs/logs/aud-citations.log
    - Exits non-zero if any cited path is missing

This script uses only Python stdlib (os, re, sys).
"""

import os
import sys
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DOCS_DIR = REPO_ROOT / "docs"
README = REPO_ROOT / "README.md"
PLAN = REPO_ROOT / "plan.md"

# Log output file
OUTPUT_LOG = REPO_ROOT / "docs" / "logs" / "aud-citations.log"


def find_citations_in_file(filepath, patterns):
    """Find all path citations in a file matching the given patterns."""
    citations = []
    try:
        with open(filepath, "r", encoding="utf8", errors="replace") as f:
            content = f.read()
    except FileNotFoundError:
        return citations

    for pattern in patterns:
        matches = re.findall(pattern, content)
        for match in matches:
            # Normalize the match - strip whitespace, remove surrounding quotes/backticks
            cleaned = match.strip().strip('`').strip('"').strip("'").strip()
            if cleaned:
                citations.append(cleaned)
    return citations


def is_valid_path_citation(text):
    """Check if the text looks like a valid repo path citation (has directory prefix)."""
    # Must contain a directory separator or be a known short path
    # Reject bare extensions or single identifiers
    if re.match(r'^[A-Za-z0-9]+$', text):
        # Single word - could be an extension or identifier, reject unless it's a known short path
        # Known single-word paths that are valid: tools, kernel, docs, mathd (as prefixes)
        # But bare "md", "py", "rs", "log" as standalone should be rejected
        return text not in {"md", "py", "rs", "log", "sh", "cfg", "toml"}
    if "/" in text or "\\" in text or text.startswith((".", "/")):
        return True
    return False


def extract_paths_from_docs():
    """Extract all file paths cited in docs/ markdown files."""
    paths = set()
    # Pattern: docs/<path> or docs\\<path> followed by a filename
    docs_patterns = [
        re.compile(r"docs[/\\][a-zA-Z0-9_./-]+/(?:\w+\.(?:log|md|py|rs|json|sh))"),
        re.compile(r"docs[/\\][a-zA-Z0-9_./-]+\.(?:log|md|py|rs|json|sh)"),
    ]

    for item in DOCS_DIR.rglob("*.md"):
        citations = find_citations_in_file(item, docs_patterns)
        for c in citations:
            if is_valid_path_citation(c):
                paths.add(c)

    # Also check .log files in docs/logs (referenced by other documents)
    for item in DOCS_DIR.rglob("*.log"):
        citations = find_citations_in_file(item, docs_patterns)
        for c in citations:
            if is_valid_path_citation(c):
                paths.add(c)

    return paths


def extract_paths_from_readme():
    """Extract file paths cited in README.md."""
    paths = set()
    # Pattern for real paths: starts with a directory like docs/, tools/, kernel/, mathd/, build/
    readme_patterns = [
        re.compile(r"(?:docs|tools|kernel|mathd|build)/[a-zA-Z0-9_./-]+\.(?:log|md|py|rs|json|sh|cfg|toml|hdd)"),
        re.compile(r"`(?:docs|tools|kernel|mathd|build)/[a-zA-Z0-9_./-]+\.(?:log|md|py|rs|json|sh)`"),
    ]

    citations = find_citations_in_file(README, readme_patterns)
    for c in citations:
        if is_valid_path_citation(c):
            paths.add(c)

    return paths


def extract_paths_from_plan():
    """Extract file paths cited in plan.md."""
    paths = set()
    plan_patterns = [
        re.compile(r"(?:docs|tools|kernel|mathd|build)/[a-zA-Z0-9_./-]+\.(?:log|md|py|rs|json|sh|cfg|toml|hdd)"),
        re.compile(r"`(?:docs|tools|kernel|mathd|build)/[a-zA-Z0-9_./-]+\.(?:log|md|py|rs|json|sh)`"),
    ]

    citations = find_citations_in_file(PLAN, plan_patterns)
    for c in citations:
        if is_valid_path_citation(c):
            paths.add(c)

    return paths


def extract_paths_from_source():
    """Extract path citations from source code comments in kernel/src/ and mathd/src/."""
    paths = set()
    source_dirs = [
        REPO_ROOT / "kernel" / "src",
        REPO_ROOT / "mathd" / "src",
    ]

    # Patterns for path citations in comments - must have a directory prefix
    source_patterns = [
        re.compile(r"(?:kernel|mathd|tools|docs)/[a-zA-Z0-9_./-]+\.(?:rs|py|md|log)"),
        re.compile(r"`(?:kernel|mathd|tools|docs)/[a-zA-Z0-9_./-]+\.(?:rs|py|md|log)`"),
    ]

    for src_dir in source_dirs:
        if not src_dir.exists():
            continue
        for item in src_dir.rglob("*.rs"):
            citations = find_citations_in_file(item, source_patterns)
            for c in citations:
                cleaned = c.strip("` ").strip()
                if cleaned and is_valid_path_citation(cleaned):
                    paths.add(cleaned)
        for item in src_dir.rglob("*.py"):
            citations = find_citations_in_file(item, source_patterns)
            for c in citations:
                cleaned = c.strip("` ").strip()
                if cleaned and is_valid_path_citation(cleaned):
                    paths.add(cleaned)

    return paths


def check_path_exists(path_str):
    """Check if a given path exists relative to the repo root."""
    # Try various resolutions
    # 1. As-is relative to repo root
    p = REPO_ROOT / path_str
    if p.exists():
        return True, str(p)

    # 2. Try as a raw path (relative to cwd)
    if os.path.exists(path_str):
        return True, path_str

    # 3. Try with repo root prepended
    if os.path.exists(REPO_ROOT / path_str):
        return True, str(REPO_ROOT / path_str)

    return False, None


def main():
    all_paths = set()

    # Collect citations from various sources
    print("Checking citations in docs/ ...")
    docs_paths = extract_paths_from_docs()
    print(f"  Found {len(docs_paths)} doc citations")
    all_paths.update(docs_paths)

    print("Checking citations in README.md ...")
    readme_paths = extract_paths_from_readme()
    print(f"  Found {len(readme_paths)} README citations")
    all_paths.update(readme_paths)

    print("Checking citations in plan.md ...")
    plan_paths = extract_paths_from_plan()
    print(f"  Found {len(plan_paths)} plan citations")
    all_paths.update(plan_paths)

    print("Checking citations in source comments ...")
    source_paths = extract_paths_from_source()
    print(f"  Found {len(source_paths)} source citations")
    all_paths.update(source_paths)

    # Now check each path exists
    missing = []
    existing = []
    for path_str in sorted(all_paths):
        exists, resolved = check_path_exists(path_str)
        if exists:
            existing.append((path_str, resolved))
        else:
            missing.append((path_str, resolved))

    # Write the log
    log_lines = []
    log_lines.append("# Citation path audit report")
    log_lines.append(f"# Generated: {__import__('datetime').datetime.now().isoformat()}")
    log_lines.append(f"# Total cited paths: {len(all_paths)}")
    log_lines.append(f"# Existing: {len(existing)}")
    log_lines.append(f"# Missing: {len(missing)}")
    log_lines.append("")

    log_lines.append("# Existing paths")
    for path_str, resolved in existing:
        log_lines.append(f"  [OK]   {path_str} -> {resolved}")
    log_lines.append("")

    log_lines.append("# Missing paths")
    if missing:
        for path_str, resolved in missing:
            log_lines.append(f"  [FAIL] {path_str} -> resolves to None")
    else:
        log_lines.append("  None")

    log_lines.append("")
    log_lines.append("# Summary")
    log_lines.append(f"Total cited paths: {len(all_paths)}")
    log_lines.append(f"Existing: {len(existing)}")
    log_lines.append(f"Missing: {len(missing)}")

    # Ensure log directory exists
    OUTPUT_LOG.parent.mkdir(parents=True, exist_ok=True)

    with open(OUTPUT_LOG, "w", encoding="utf8") as f:
        f.write("\n".join(log_lines))

    print(f"\nReport written to {OUTPUT_LOG}")
    print(f"Total cited paths: {len(all_paths)}")
    print(f"Existing: {len(existing)}")
    print(f"Missing: {len(missing)}")

    if missing:
        print("\nMISSING PATHS (will cause non-zero exit):")
        for path_str, resolved in missing:
            print(f"  - {path_str}")
        sys.exit(1)
    else:
        print("\nAll cited paths exist!")
        sys.exit(0)


if __name__ == "__main__":
    main()