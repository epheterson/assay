"""
state-via-issues — Store assay's per-target state in the repo's own Issues.

Each verdict opens an issue labeled `assay:{owner}/{repo}` plus a verdict-class
label (`clean` / `review` / `blocked`). State = the most recent such issue.

Why issues:
  - Atomic via GitHub API (no commit-conflict races between parallel runs)
  - Audit log + state + notification UX collapsed into one mechanism
  - Subscribing to label triggers native GitHub mobile pushes — solves the
    "GitHub app notification" path for free

Body format (machine-readable section delimited by HTML comments so humans
can scroll past it):

    <!-- ASSAY-STATE
    {
      "tag": "v1.2.3",
      "verdict": "clean",
      "score": 9,
      "judged_at": "2026-05-20T07:00:00Z",
      "tracked_owner": "Balackburn",
      "tracked_repo": "Apollo"
    }
    ASSAY-STATE -->
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GH_API = "https://api.github.com"
STATE_RE = re.compile(r"<!--\s*ASSAY-STATE\s*(\{.*?\})\s*ASSAY-STATE\s*-->", re.S)


def gh(
    method: str, path: str, body: dict | None = None, token: str | None = None
) -> dict | list:
    url = f"{GH_API}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        resp_bytes = r.read()
        return json.loads(resp_bytes) if resp_bytes else {}


def label_for(tracked_owner: str, tracked_repo: str) -> str:
    return f"assay:{tracked_owner}/{tracked_repo}"


def ensure_labels(
    assay_owner: str, assay_repo: str, tracked_owner: str, tracked_repo: str, token: str
) -> None:
    """Create labels if they don't exist. Idempotent."""
    wanted = [
        (
            label_for(tracked_owner, tracked_repo),
            "0e8a16",
            f"assay target: {tracked_owner}/{tracked_repo}",
        ),
        ("verdict:clean", "0e8a16", "release looks consistent with stated intent"),
        ("verdict:review", "fbca04", "anomalies worth a human look"),
        ("verdict:blocked", "b60205", "high-confidence problem; do not auto-update"),
        ("baseline", "c5def5", "initial baseline; not a verdict"),
    ]
    for name, color, desc in wanted:
        body = {"name": name, "color": color, "description": desc[:99]}
        try:
            gh("POST", f"/repos/{assay_owner}/{assay_repo}/labels", body, token)
        except urllib.error.HTTPError as e:
            if e.code not in (422,):
                raise


def get_last_state(
    assay_owner: str, assay_repo: str, tracked_owner: str, tracked_repo: str, token: str
) -> dict | None:
    """Return the parsed state of the most recent issue for this target, or None."""
    lbl = urllib.parse.quote(label_for(tracked_owner, tracked_repo), safe="")
    issues = gh(
        "GET",
        f"/repos/{assay_owner}/{assay_repo}/issues?state=all&labels={lbl}&per_page=1&sort=created&direction=desc",
        token=token,
    )
    if not issues:
        return None
    body = issues[0].get("body") or ""
    m = STATE_RE.search(body)
    if not m:
        return None
    try:
        state = json.loads(m.group(1))
        state["_issue_number"] = issues[0]["number"]
        state["_issue_url"] = issues[0]["html_url"]
        return state
    except json.JSONDecodeError:
        return None


def write_verdict_issue(
    *,
    assay_owner: str,
    assay_repo: str,
    tracked_owner: str,
    tracked_repo: str,
    tag: str,
    baseline_tag: str | None,
    verdict: dict,
    release_url: str,
    findings: list[dict],
    token: str,
) -> dict:
    """Open a new issue carrying the verdict + state. Auto-close if clean."""
    v_class = verdict.get("verdict", "review")
    score = verdict.get("score", "?")
    emoji = {"clean": "✅", "review": "⚠️", "blocked": "🚨"}.get(v_class, "❓")
    headline = verdict.get("headline", "(no headline)")
    title = f"[assay] {emoji} {tracked_owner}/{tracked_repo} {tag} — {v_class}"

    state = {
        "tag": tag,
        "baseline_tag": baseline_tag,
        "verdict": v_class,
        "score": score,
        "judged_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tracked_owner": tracked_owner,
        "tracked_repo": tracked_repo,
    }

    anomalies_md = ""
    for a in verdict.get("anomalies", []) or []:
        anomalies_md += (
            f"- **{a.get('severity','?').upper()}** "
            f"({a.get('module','?')}): {a.get('what','')}. "
            f"_{a.get('why_it_matters','')}_\n"
        )
    if not anomalies_md:
        anomalies_md = "_(none)_"

    decision_inputs_md = ""
    for d in verdict.get("decision_inputs", []) or []:
        decision_inputs_md += f"- {d}\n"
    if not decision_inputs_md:
        decision_inputs_md = "_(none provided)_"

    links_md = ""
    extra = verdict.get("links", {}) or {}
    if extra.get("code_compare_url"):
        links_md += f"- Code diff: {extra['code_compare_url']}\n"
    if extra.get("release_compare_url"):
        links_md += f"- Release diff: {extra['release_compare_url']}\n"
    if extra.get("release_url"):
        links_md += f"- Release page: {extra['release_url']}\n"
    if not links_md:
        links_md = "_(none)_"

    findings_md = "<details><summary>Module findings (JSON)</summary>\n\n```json\n"
    findings_md += json.dumps(findings, indent=2)[:50000]
    findings_md += "\n```\n</details>"

    body = (
        f"## {emoji} {headline}\n\n"
        f"**Verdict:** `{v_class}` (score {score}/10)\n"
        f"**Tag:** [`{tag}`]({release_url})\n"
        f"**Baseline:** `{baseline_tag or '(none — first run)'}`\n"
        f"**Judge:** `{verdict.get('judge_source','?')}`\n\n"
        f"### Reasoning\n{verdict.get('reasoning','(none)')}\n\n"
        f"### What to check before deciding\n{decision_inputs_md}\n"
        f"### Quick links\n{links_md}\n"
        f"### Anomalies\n{anomalies_md}\n\n"
        f"### Findings\n{findings_md}\n\n"
        f"---\n"
        f"<!-- ASSAY-STATE\n{json.dumps(state)}\nASSAY-STATE -->\n"
    )

    labels = [label_for(tracked_owner, tracked_repo), f"verdict:{v_class}"]

    ensure_labels(assay_owner, assay_repo, tracked_owner, tracked_repo, token)

    issue = gh(
        "POST",
        f"/repos/{assay_owner}/{assay_repo}/issues",
        {"title": title, "body": body, "labels": labels},
        token,
    )

    if v_class == "clean":
        gh(
            "PATCH",
            f"/repos/{assay_owner}/{assay_repo}/issues/{issue['number']}",
            {"state": "closed"},
            token,
        )

    return issue


def write_baseline_issue(
    *,
    assay_owner: str,
    assay_repo: str,
    tracked_owner: str,
    tracked_repo: str,
    tag: str,
    release_url: str,
    token: str,
) -> dict:
    """Open a baseline issue to establish initial state — no verdict, no LLM call."""
    state = {
        "tag": tag,
        "baseline_tag": None,
        "verdict": "baseline",
        "score": None,
        "judged_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tracked_owner": tracked_owner,
        "tracked_repo": tracked_repo,
    }
    title = f"[assay] 🪙 {tracked_owner}/{tracked_repo} {tag} — baseline"
    body = (
        f"## 🪙 Baseline established\n\n"
        f"First observation of `{tracked_owner}/{tracked_repo}`. "
        f"Future releases will be diffed against this tag.\n\n"
        f"**Tag:** [`{tag}`]({release_url})\n\n"
        f"---\n"
        f"<!-- ASSAY-STATE\n{json.dumps(state)}\nASSAY-STATE -->\n"
    )
    ensure_labels(assay_owner, assay_repo, tracked_owner, tracked_repo, token)
    issue = gh(
        "POST",
        f"/repos/{assay_owner}/{assay_repo}/issues",
        {
            "title": title,
            "body": body,
            "labels": [label_for(tracked_owner, tracked_repo), "baseline"],
        },
        token,
    )
    gh(
        "PATCH",
        f"/repos/{assay_owner}/{assay_repo}/issues/{issue['number']}",
        {"state": "closed"},
        token,
    )
    return issue


def main() -> int:
    """CLI for testing.

    Usage: state_via_issues.py get OWNER REPO TRACKED_OWNER TRACKED_REPO
    """
    if len(sys.argv) < 2 or sys.argv[1] not in ("get",):
        print(
            "usage: state_via_issues.py get OWNER REPO TRACKED_OWNER TRACKED_REPO",
            file=sys.stderr,
        )
        return 2
    _, _cmd, owner, repo, t_owner, t_repo = sys.argv
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN required", file=sys.stderr)
        return 1
    state = get_last_state(owner, repo, t_owner, t_repo, token)
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    import urllib.error  # noqa: F401 (used in inner scope)

    sys.exit(main())
