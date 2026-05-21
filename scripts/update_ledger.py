"""
update_ledger — Regenerate LEDGER.md from the verdicts/ directory.

LEDGER.md is the human-readable history of every verdict assay has rendered.
One table per tracked repo, newest first. Committed alongside each verdict
in the same workflow run.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

VERDICT_EMOJI = {"clean": "✅", "review": "⚠️", "blocked": "🚨", "baseline": "🪙"}


def collect_verdicts(verdicts_dir: Path) -> dict[str, list[dict]]:
    """Group verdicts by tracked repo. Each entry has tag, verdict, score, judged_at, headline, issue link if known."""
    by_target: dict[str, list[dict]] = {}
    for p in sorted(verdicts_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        v = data.get("verdict", {})
        tag = data.get("tag", "?")
        baseline = data.get("baseline", "?")
        # Derive target from filename: {owner}-{repo}-{tag}.json
        # Use the verdict's tracked fields if present in the dict; else parse filename
        stem = p.stem
        # filename format: {owner}-{repo}-{safe_tag}.json
        # Tracked owner/repo aren't in the JSON; reconstruct from filename heuristically.
        parts = stem.split("-", 2)
        if len(parts) >= 3:
            owner, repo, _ = parts[0], parts[1], parts[2]
            target = f"{owner}/{repo}"
        else:
            target = "unknown"
        entry = {
            "tag": tag,
            "baseline": baseline,
            "verdict": v.get("verdict", "?"),
            "score": v.get("score"),
            "headline": v.get("headline", ""),
            "judge_source": v.get("judge_source", ""),
            "judged_at": v.get("judged_at")
            or v.get("links", {}).get("release_url", ""),
            "release_url": v.get("links", {}).get("release_url", ""),
        }
        by_target.setdefault(target, []).append(entry)
    return by_target


def render_ledger(by_target: dict[str, list[dict]]) -> str:
    md = (
        "# assay ledger\n\n"
        "Every verdict, every release, every target. Newest first. Auto-generated on each verdict.\n\n"
    )
    if not by_target:
        md += "_(no verdicts yet)_\n"
        return md

    total = sum(len(v) for v in by_target.values())
    clean = sum(1 for v in by_target.values() for e in v if e["verdict"] == "clean")
    review = sum(1 for v in by_target.values() for e in v if e["verdict"] == "review")
    blocked = sum(1 for v in by_target.values() for e in v if e["verdict"] == "blocked")
    md += f"**Totals:** {total} verdicts · "
    md += f"✅ {clean} clean · ⚠️ {review} review · 🚨 {blocked} blocked\n\n"

    for target in sorted(by_target):
        entries = sorted(
            by_target[target],
            key=lambda e: e.get("tag", ""),
            reverse=True,
        )
        md += f"## {target}\n\n"
        md += "| | Tag | Score | Headline | Judge | Baseline |\n"
        md += "|---|---|---|---|---|---|\n"
        for e in entries:
            emoji = VERDICT_EMOJI.get(e["verdict"], "❓")
            score = e["score"] if e["score"] is not None else "—"
            tag_link = (
                f"[`{e['tag']}`]({e['release_url']})"
                if e.get("release_url")
                else f"`{e['tag']}`"
            )
            md += (
                f"| {emoji} `{e['verdict']}` | {tag_link} | {score} | "
                f"{e['headline'][:80]} | `{e['judge_source']}` | `{e.get('baseline','?')}` |\n"
            )
        md += "\n"

    md += (
        "---\n\n"
        "_Each row links to the upstream release. The corresponding verdict issue "
        "lives at https://github.com/epheterson/assay/issues — filter by the "
        "`assay:{owner}/{repo}` label for a specific target._\n"
    )
    return md


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    verdicts_dir = root / "verdicts"
    by_target = collect_verdicts(verdicts_dir)
    ledger_md = render_ledger(by_target)
    (root / "LEDGER.md").write_text(ledger_md)
    print(f"wrote LEDGER.md with {sum(len(v) for v in by_target.values())} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
