#!/usr/bin/env python3
"""
team_times.py

Check that message times in docs/team.md match their git commit times.
Reports messages where stated time differs from commit time by > 30 minutes.
Lines with IST or no timezone are listed separately as "not checkable".
"""

import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


TEAM_MD = Path("docs/team.md")


def parse_team_messages():
    """Extract message lines with timestamps from team.md."""
    if not TEAM_MD.exists():
        print(f"Error: {TEAM_MD} not found", file=sys.stderr)
        sys.exit(1)

    pattern = re.compile(
        r'^-\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+(?:UTC|IST))\s+(.+)$'
    )
    messages = []
    with TEAM_MD.open() as f:
        for lineno, line in enumerate(f, 1):
            m = pattern.match(line)
            if m:
                ts_str, rest = m.groups()
                messages.append((lineno, ts_str, rest.strip()))
    return messages


def parse_timestamp(ts_str):
    """Parse timestamp string to datetime (UTC). Returns (dt, zone) or (None, zone)."""
    ts_str = ts_str.strip()
    if ts_str.endswith(" UTC"):
        dt = datetime.strptime(ts_str[:-4], "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=timezone.utc), "UTC"
    elif ts_str.endswith(" IST"):
        dt = datetime.strptime(ts_str[:-4], "%Y-%m-%d %H:%M")
        # IST = UTC+5:30
        dt = dt.replace(tzinfo=timezone.utc)
        return dt, "IST"
    else:
        return None, "NONE"


def get_commit_time(line_no):
    """Get commit timestamp for a line using git blame --line-porcelain."""
    try:
        result = subprocess.run(
            ["git", "blame", "--line-porcelain", "-L", f"{line_no},{line_no}", str(TEAM_MD)],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.splitlines():
            if line.startswith("committer-time "):
                ts = int(line.split()[1])
                return datetime.fromtimestamp(ts, tz=timezone.utc)
    except subprocess.CalledProcessError:
        pass
    return None


def main():
    messages = parse_team_messages()
    findings = []
    not_checkable = []

    for lineno, ts_str, rest in messages:
        dt, zone = parse_timestamp(ts_str)
        if zone != "UTC":
            not_checkable.append((lineno, ts_str, rest, zone))
            continue

        commit_dt = get_commit_time(lineno)
        if commit_dt is None:
            not_checkable.append((lineno, ts_str, rest, "NO_COMMIT"))
            continue

        diff = abs((dt - commit_dt).total_seconds()) / 60  # minutes
        if diff > 30:
            findings.append((lineno, ts_str, commit_dt.strftime("%Y-%m-%d %H:%M UTC"), int(diff), rest))

    # Output findings
    for lineno, stated, commit, diff, rest in findings:
        print(f"{TEAM_MD}:{lineno}: stated {stated} vs commit {commit} (diff {diff} min) -> {rest}")

    # Output not checkable
    for lineno, ts_str, rest, zone in not_checkable:
        print(f"{TEAM_MD}:{lineno}: not checkable ({zone}) -> {rest}")

    # Final count
    print(f"findings: {len(findings)} not_checkable: {len(not_checkable)}")

    # Exit 1 if findings, 0 otherwise
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()