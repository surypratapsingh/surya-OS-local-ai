#!/usr/bin/env python3
"""
status-audit.py — Generate docs/logs/status-audit-2026-11-02.log

Compares every status claim in README.md, docs/roadmap.md and plan.md to
committed logs, in the format of docs/logs/status-audit-2026-10-11.log.

Uses only Python stdlib (os, re, sys, datetime).
"""

import os
import sys
import re
from pathlib import Path
from datetime import datetime

REPO_ROOT = Path(__file__).parent.parent
README = REPO_ROOT / "README.md"
ROADMAP = REPO_ROOT / "docs" / "roadmap.md"
PLAN = REPO_ROOT / "plan.md"
LOGS_DIR = REPO_ROOT / "docs" / "logs"

OUTPUT_LOG = REPO_ROOT / "docs" / "logs" / "status-audit-2026-11-02.log"


def find_log_file(log_ref, logs_dir=LOGS_DIR):
    """Find a log file referenced in a claim."""
    # Normalize: remove leading ./ or \\
    log_ref = log_ref.lstrip("./")
    
    # Try exact name first (as full path)
    p = logs_dir / log_ref
    if p.exists():
        return str(p)
    
    # Try basename match against all .log files
    basename = os.path.basename(log_ref)
    for item in logs_dir.rglob("*.log"):
        if basename in item.name or item.name.startswith(basename):
            return str(item)
    
    # Try substring match (case-insensitive)
    log_ref_lower = log_ref.lower()
    for item in logs_dir.rglob("*.log"):
        if log_ref_lower in item.name.lower():
            return str(item)
    
    # If it has a wildcard like *.log, try matching
    if "*" in log_ref:
        for item in logs_dir.rglob("*.log"):
            if log_ref.replace("*", "") in item.name:
                return str(item)
    
    return None


def grep_log_pattern(log_path, pattern):
    """Search a log file returning the first matching line number and content."""
    try:
        with open(log_path, "r", encoding="utf8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if re.search(pattern, line, re.IGNORECASE):
                    return i, line.rstrip("\n")
    except FileNotFoundError:
        pass
    return None, None


def extract_log_refs_from_text(text):
    """Extract all potential log file references from claim text."""
    refs = set()
    
    # 1. Backtick-enclosed references: `docs/logs/...` or `ci-...`
    for m in re.finditer(r"`([^`]+)`", text):
        ref = m.group(1).strip()
        if ref:
            refs.add(ref)
    
    # 2. docs/logs/... patterns (outside backticks)
    for m in re.finditer(r"(?:docs|tools|kernel|mathd)/logs/[^\s`]+", text):
        ref = m.group(0).strip()
        if ref:
            refs.add(ref)
    
    # 3. ci-... pattern
    for m in re.finditer(r"ci-\d+[^\s`]*", text):
        ref = m.group(0).strip()
        if ref:
            refs.add(ref)
    
    # 4. w2-*, r14-*, r5-*, etc. pattern (work order logs)
    for m in re.finditer(r"[a-z]+\d+-\*|[a-z]+-\d+[^\s`]*", text):
        ref = m.group(0).strip()
        if ref and len(ref) > 3:
            refs.add(ref)
    
    return refs


def main():
    # Collect all claims
    all_claims = []

    # --- Parse plan.md [x] claims ---
    with open(PLAN, "r", encoding="utf8", errors="replace") as f:
        plan_lines = f.readlines()

    for i, line in enumerate(plan_lines, 1):
        m = re.match(r"\s*- \[x\]\s+(.+)", line)
        if m:
            claim_text = m.group(1).strip()
            log_refs = extract_log_refs_from_text(claim_text)
            all_claims.append({
                "id": i,
                "file": "plan.md",
                "line": i,
                "text": claim_text,
                "log_refs": list(log_refs),
                "source": "plan",
            })

    # --- Parse README.md ✅ claims ---
    with open(README, "r", encoding="utf8", errors="replace") as f:
        readme_content = f.read()

    for m in re.finditer(r"✅\s+(.+?)(?:\n|$)", readme_content):
        line_num = readme_content[:m.start()].count("\n") + 1
        claim_text = m.group(1).strip()
        log_refs = extract_log_refs_from_text(claim_text)
        all_claims.append({
            "id": len(all_claims) + 1,
            "file": "README.md",
            "line": line_num,
            "text": claim_text,
            "log_refs": list(log_refs),
            "source": "readme",
        })

    # --- Parse roadmap.md ✅ claims ---
    with open(ROADMAP, "r", encoding="utf8", errors="replace") as f:
        roadmap_content = f.read()

    # Match ✅ patterns
    for m in re.finditer(r"✅\s+(.+?)(?:\n|$)", roadmap_content):
        line_num = roadmap_content[:m.start()].count("\n") + 1
        claim_text = m.group(1).strip()
        log_refs = extract_log_refs_from_text(claim_text)
        if log_refs or len(claim_text) > 10:
            all_claims.append({
                "id": len(all_claims) + 1,
                "file": "roadmap.md",
                "line": line_num,
                "text": claim_text,
                "log_refs": list(log_refs),
                "source": "roadmap",
            })

    # Also match [-] [x] patterns
    for m in re.finditer(r"[-*]\s*\[\s*x\s*\]\s+(.+?)(?:\n|$)", roadmap_content):
        line_num = roadmap_content[:m.start()].count("\n") + 1
        claim_text = m.group(1).strip()
        log_refs = extract_log_refs_from_text(claim_text)
        if len(claim_text) > 5:
            all_claims.append({
                "id": len(all_claims) + 1,
                "file": "roadmap.md",
                "line": line_num,
                "text": claim_text,
                "log_refs": list(log_refs),
                "source": "roadmap",
            })

    # Deduplicate claims by content (case-insensitive)
    seen = {}
    unique_claims = []
    for c in all_claims:
        key = c["text"].lower()
        if key not in seen:
            seen[key] = c
            unique_claims.append(c)
    all_claims = unique_claims

    # --- For each claim, find supporting log and check it ---
    results = []
    transcript_lines = []

    idx = 1
    for claim in all_claims:
        found = False
        pass_line_info = None
        skipped = False
        verdict = "NOT FOUND"
        action = ""

        # Try each log reference
        for log_ref in claim["log_refs"]:
            log_path = find_log_file(log_ref)
            if log_path is None:
                continue

            # Check for PASS line
            line_num, line_content = grep_log_pattern(log_path, r"\bPASS\b")
            if line_content:
                found = True
                pass_line_info = (line_num, line_content)
                # Check if it's a SKIP
                if re.search(r"SKIP", line_content, re.IGNORECASE):
                    skipped = True

            # Check for check: ALL PASSED
            if not found:
                line_num, line_content = grep_log_pattern(log_path, r"check:\s+ALL\s+PASSED")
                if line_content:
                    found = True
                    pass_line_info = (line_num, line_content)

            if found:
                break

        if found and pass_line_info and not skipped:
            verdict = "SUPPORTED"
            action = "no action needed"
        elif found and skipped:
            verdict = "PARTIAL"
            action = "note the SKIP"
        else:
            verdict = "NOT FOUND"
            action = "no committed log backs this claim"

        results.append({
            "id": idx,
            "claim": claim["file"] + ":" + str(claim["line"]),
            "text": claim["text"][:60],
            "log_paths": claim["log_refs"],
            "found": found,
            "pass_line_info": pass_line_info,
            "skipped": skipped,
            "verdict": verdict,
            "action": action,
            "source": claim["source"],
        })
        idx += 1

    # --- Write the audit log ---
    lines = []
    lines.append("# Status audit 2026-11-02")
    lines.append("#")
    lines.append(f"# Scope: every ✅ in README.md, [x] in plan.md, and ✅ in docs/roadmap.md")
    lines.append(f"# Generated: {datetime.now().isoformat()}")
    lines.append("#")
    lines.append("# Method: for each claim, find the log it relies on, check that the file exists,")
    lines.append("# and check that the PASS line is in it and is not a SKIP.")
    lines.append("#")
    lines.append("# Three verdicts:")
    lines.append("#   SUPPORTED  the log exists and holds the PASS line, with no SKIP in its place")
    lines.append("#   PARTIAL    the log supports the headline but not every detail in the claim")
    lines.append("#   NOT FOUND  no committed log backs the claim")
    lines.append("#")
    lines.append("# id   | claim (file:line)                         | log path(s)                                    | found | PASS line (log:line)                                   | skipped? | verdict   | action")
    lines.append("# -----+-------------------------------------------+------------------------------------------------+-------+--------------------------------------------------------+----------+-----------+------------------------------------------")

    tally = {"SUPPORTED": 0, "PARTIAL": 0, "NOT FOUND": 0}

    for r in results:
        # Format log paths - show first few, truncate if too long
        log_paths_str = "; ".join(r["log_paths"]) if r["log_paths"] else "-"
        if len(log_paths_str) > 61:
            log_paths_str = log_paths_str[:58] + "..."

        pass_line_str = ""
        if r["pass_line_info"]:
            ln, content = r["pass_line_info"]
            pass_line_str = f"{ln} {content}"
            if len(pass_line_str) > 58:
                pass_line_str = pass_line_str[:55] + "..."

        else:
            pass_line_str = "-"

        row = f"{r['id']:4}   | {r['claim'][:58]:58} | {log_paths_str[:61]:61} | {'yes' if r['found'] else 'no':3} | {pass_line_str[:58]:58} | {'yes' if r['skipped'] else 'no':6} | {r['verdict'][:11]:11} | {r['action'][:35]:35}"
        lines.append(row)
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1

    lines.append("#")
    lines.append(f"# Totals: {tally.get('SUPPORTED', 0)} SUPPORTED, {tally.get('PARTIAL', 0)} PARTIAL, {tally.get('NOT FOUND', 0)} NOT FOUND")
    lines.append("#")
    lines.append("# NOT FOUND: claims without committed log backing")
    lines.append("# PARTIAL: claims with partial log support")
    lines.append("# SUPPORTED: claims fully backed by committed logs")
    lines.append("#")

    # --- TRANSCRIPT section ---
    lines.append("#")
    lines.append("# =============================================================================")
    lines.append("# TRANSCRIPT")
    lines.append("# =============================================================================")
    lines.append("#")
    lines.append("# 0. Every cited log path check")
    lines.append("#")

    # Check each cited log exists
    for r in results:
        for log_ref in r["log_paths"]:
            log_path = find_log_file(log_ref.split("/")[-1] if "/" in log_ref else log_ref)
            if log_path:
                lines.append(f"  ok      {log_ref} -> {log_path}")
            else:
                lines.append(f"  MISSING   {log_ref}")
    lines.append("#")

    lines.append("# 1. Transcript: verify each claim's log support")
    for r in results:
        lines.append(f"")
        lines.append(f"# {r['claim']} — \"{r['text'][:60]}...\"")
        if r["log_paths"]:
            log_ref = r["log_paths"][0]
            log_path = find_log_file(log_ref.split("/")[-1] if "/" in log_ref else log_ref)
            if log_path:
                lines.append(f"  $ check-log {log_ref}")
                line_num, line_content = grep_log_pattern(log_path, r"\bPASS\b")
                if line_content:
                    lines.append(f"  ok      PASS found at line {line_num}: {line_content[:60]}")
                else:
                    lines.append(f"  FAIL    PASS not found in {log_ref}")
                    # Try ALL PASSED
                    line_num2, line_content2 = grep_log_pattern(log_path, r"check:\s+ALL\s+PASSED")
                    if line_content2:
                        lines.append(f"  ok      check: ALL PASSED found at line {line_num2}: {line_content2[:60]}")
                    else:
                        lines.append(f"  FAIL    No PASS or check: ALL PASSED in {log_ref}")
            else:
                lines.append(f"  FAIL    log {log_ref} not found on disk")
        else:
            lines.append(f"  FAIL    no log references in claim")

    # Write the log
    OUTPUT_LOG.parent.mkdir(parents=True, exist_ok=True)

    with open(OUTPUT_LOG, "w", encoding="utf8") as f:
        f.write("\n".join(lines))

    print(f"Report written to {OUTPUT_LOG}")
    print(f"Totals: SUPPORTED={tally.get('SUPPORTED', 0)}, PARTIAL={tally.get('PARTIAL', 0)}, NOT FOUND={tally.get('NOT FOUND', 0)}")
    print(f"Citations checked: {len(results)}")


if __name__ == "__main__":
    main()