"""
url-inventory — Extract hostnames from a binary or source tree and diff
against a baseline.

Hosts are the most concrete proxy for "what does this program talk to."
Surfaces:
  - added_hosts: present in new release, absent from baseline
  - removed_hosts: gone in new release
  - all_hosts: union, for full audit
  - hard_flag: True if any added host is not on a configured allowlist

Allowlist is a JSON file listing hosts considered safe-and-expected for the
target. For Apollo: reddit.com, imgur.com, github.com, etc.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Matches http(s)://hostname (no path). Hostname must contain at least one dot.
URL_RE = re.compile(rb"https?://([a-zA-Z0-9][a-zA-Z0-9.-]+\.[a-zA-Z]{2,})")


def hosts_from_strings(blob: bytes) -> set[str]:
    """Extract unique hostnames from a binary blob using a regex.

    Avoids shelling out to `strings`; works on any binary or text content.
    """
    found: set[str] = set()
    for m in URL_RE.finditer(blob):
        host = m.group(1).decode("ascii", errors="ignore").lower().rstrip(".")
        # filter obvious noise: trailing punctuation, comma-glued addresses
        host = (
            host.split(",")[0].split(")")[0].split("]")[0].split('"')[0].split("'")[0]
        )
        if len(host) > 4 and "." in host:
            found.add(host)
    return found


def hosts_from_path(path: Path) -> set[str]:
    """Scan a file or directory tree for URL hostnames."""
    found: set[str] = set()
    if path.is_file():
        try:
            data = path.read_bytes()
            found |= hosts_from_strings(data)
        except OSError:
            pass
    elif path.is_dir():
        for child in path.rglob("*"):
            if child.is_file() and child.stat().st_size < 200 * 1024 * 1024:
                try:
                    data = child.read_bytes()
                    found |= hosts_from_strings(data)
                except OSError:
                    pass
    return found


def load_allowlist(path: Path | None) -> set[str]:
    if not path or not path.exists():
        return set()
    raw = json.loads(path.read_text())
    return {h.lower() for h in raw.get("hosts", [])}


def diff(baseline_hosts: set[str], new_hosts: set[str]) -> dict:
    added = sorted(new_hosts - baseline_hosts)
    removed = sorted(baseline_hosts - new_hosts)
    common = sorted(baseline_hosts & new_hosts)
    return {"added": added, "removed": removed, "common": common}


def run(
    new_path: Path,
    baseline_path: Path | None = None,
    allowlist_path: Path | None = None,
) -> dict:
    new_hosts = hosts_from_path(new_path)
    baseline_hosts = hosts_from_path(baseline_path) if baseline_path else set()
    allow = load_allowlist(allowlist_path)
    d = diff(baseline_hosts, new_hosts)

    unallowlisted_additions = [h for h in d["added"] if h not in allow]
    hard_flag = bool(unallowlisted_additions) and baseline_path is not None

    return {
        "module": "url_inventory",
        "ok": True,
        "new_path": str(new_path),
        "baseline_path": str(baseline_path) if baseline_path else None,
        "allowlist_path": str(allowlist_path) if allowlist_path else None,
        "total_hosts": len(new_hosts),
        "added": d["added"],
        "removed": d["removed"],
        "added_unallowlisted": unallowlisted_additions,
        "hard_flag": hard_flag,
        "summary": (
            f"{len(new_hosts)} total hosts. "
            f"{len(d['added'])} added ({len(unallowlisted_additions)} not on allowlist), "
            f"{len(d['removed'])} removed."
        ),
    }


def main() -> int:
    """CLI entrypoint for local testing.

    Usage: python url_inventory.py NEW_PATH [BASELINE_PATH [ALLOWLIST_PATH]]
    """
    if len(sys.argv) < 2:
        print(
            "usage: url_inventory.py NEW_PATH [BASELINE_PATH [ALLOWLIST_PATH]]",
            file=sys.stderr,
        )
        return 2
    new = Path(sys.argv[1])
    base = Path(sys.argv[2]) if len(sys.argv) >= 3 else None
    allow = Path(sys.argv[3]) if len(sys.argv) >= 4 else None
    result = run(new, base, allow)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
