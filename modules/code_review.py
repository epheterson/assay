"""
code_review — Per-commit security/privacy/safety review for the window
between two refs of a code repo.

For each commit, fetch metadata (title, body, author, files, +/- line counts)
and have the LLM produce a structured summary:

  {
    "sha": "abc123de",
    "title": "Block statsigapi.net (Statsig SDK exception endpoint)",
    "author": "epheterson",
    "files_touched": ["Tweak.xm"],
    "kind": "security" | "feature" | "fix" | "refactor" | "docs" | "test" | "config" | "ui" | "other",
    "areas": ["networking", "ui", "auth", "storage", "crypto", "permissions", ...],
    "summary": "<2-3 sentence plain-English summary of what this commit does>",
    "security_implications": "<why a reviewer cares, or '(none)'>",
    "privacy_implications": "<why a reviewer cares, or '(none)'>",
    "safety_implications": "<crashes / data loss / etc., or '(none)'>",
    "risk_signals": ["list of strings"],
    "severity": "none" | "low" | "medium" | "high"
  }

Sends commits to GitHub Models in batches (default 5 per call) for efficiency.
Falls back to a heuristic-only summary if no LLM token is available.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

GH_API = "https://api.github.com"
MODELS_URL = "https://models.github.ai/inference/chat/completions"

# Add judge/ to sys.path so we can reuse the prompt loader + extractor
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR / "judge"))


def gh_get(path: str, token: str) -> dict | list:
    req = urllib.request.Request(f"{GH_API}{path}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def list_commits(owner: str, repo: str, base: str, head: str, token: str) -> list[dict]:
    """Return the list of commits in head not in base, with stats and files."""
    data = gh_get(f"/repos/{owner}/{repo}/compare/{base}...{head}", token)
    commits = data.get("commits", [])
    # The compare endpoint includes basic commit info but NOT file stats.
    # Fetch each commit individually for stats — small extra cost, much
    # richer signal for the LLM.
    enriched: list[dict] = []
    for c in commits:
        sha = c["sha"]
        try:
            detail = gh_get(f"/repos/{owner}/{repo}/commits/{sha}", token)
        except urllib.error.HTTPError:
            detail = c
        enriched.append(
            {
                "sha": sha,
                "short_sha": sha[:8],
                "url": detail.get("html_url"),
                "author_login": (detail.get("author") or {}).get("login")
                or "(unknown)",
                "committer_login": (detail.get("committer") or {}).get("login"),
                "message": detail.get("commit", {}).get("message", ""),
                "stats": detail.get("stats", {}),
                "files": [
                    {
                        "filename": f.get("filename"),
                        "status": f.get("status"),
                        "additions": f.get("additions", 0),
                        "deletions": f.get("deletions", 0),
                    }
                    for f in (detail.get("files") or [])[
                        :50
                    ]  # cap to avoid blowing prompt
                ],
            }
        )
    return enriched


def call_llm(
    *, model: str, system: str, user: str, token: str, max_tokens: int = 4000
) -> str:
    body = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        MODELS_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_text = "(no body)"
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        raise urllib.error.HTTPError(
            e.url, e.code, f"{e.reason} :: {body_text}", e.headers, None
        ) from e
    return data["choices"][0]["message"]["content"]


REVIEW_SYSTEM = """You are assay's commit-by-commit security/privacy/safety reviewer.

For each commit you are given (title + body + files touched + line counts), produce a structured json review. Be specific and concise — every field should be useful for a human deciding whether to ship this release.

Focus angles (look explicitly for each, every commit):
- Security: does this touch auth, crypto, signing, network endpoints, secrets, input validation, deserialization, file permissions?
- Privacy: does this change what data is collected, where it goes, what's logged, what's persisted, any telemetry, any identifiers?
- Safety: does this change crash-prone areas, data-persistence, threading, memory management, error handling?

If a commit is plainly a docs/UI/build/asset-only change with no security/privacy/safety angle, say so concisely — don't manufacture concerns.

Severity guidance:
- "none"   = docs, formatting, UI tweaks with zero security/privacy/safety surface
- "low"    = UI/UX change that touches some sensitive code path but obviously safe
- "medium" = real changes to network/auth/storage/permissions worth a human glance
- "high"   = unexplained network endpoint, new secret-handling, obfuscated code, expanded permissions inconsistent with stated intent

Output ONLY a single json object of the form:
{
  "commits": [
    {
      "sha": "...",
      "title": "<first non-blank line of commit message>",
      "author": "<login>",
      "files_touched": ["..."],
      "kind": "security" | "feature" | "fix" | "refactor" | "docs" | "test" | "config" | "ui" | "build" | "asset" | "other",
      "areas": ["networking" | "ui" | "auth" | "storage" | "crypto" | "permissions" | "ipc" | "threading" | "build" | "deps" | ...],
      "summary": "<2-3 plain-English sentences>",
      "security_implications": "<one sentence, or '(none)'>",
      "privacy_implications": "<one sentence, or '(none)'>",
      "safety_implications": "<one sentence, or '(none)'>",
      "risk_signals": ["any specific phrases / patterns of concern"],
      "severity": "none" | "low" | "medium" | "high"
    },
    ...
  ]
}

Reply with json only. No prose. Start with `{`, end with `}`."""


def heuristic_review(commit: dict) -> dict:
    """No-LLM fallback: produce a low-fi review from commit metadata."""
    msg = commit.get("message", "")
    title = msg.split("\n", 1)[0][:120]
    files = [f["filename"] for f in commit.get("files") or [] if f.get("filename")]
    return {
        "sha": commit["sha"],
        "short_sha": commit["short_sha"],
        "title": title,
        "author": commit.get("author_login", "(unknown)"),
        "files_touched": files[:10],
        "kind": "other",
        "areas": [],
        "summary": title,
        "security_implications": "(heuristic; no LLM review available)",
        "privacy_implications": "(heuristic; no LLM review available)",
        "safety_implications": "(heuristic; no LLM review available)",
        "risk_signals": [],
        "severity": "none",
        "url": commit.get("url"),
    }


def review_batch(commits_batch: list[dict], model: str, token: str) -> list[dict]:
    """Send one LLM call covering up to ~5 commits, return parsed reviews."""
    payload = [
        {
            "sha": c["sha"],
            "short_sha": c["short_sha"],
            "author": c.get("author_login"),
            "message": c.get("message", "")[:2000],
            "stats": c.get("stats"),
            "files": c.get("files"),
            "url": c.get("url"),
        }
        for c in commits_batch
    ]
    user = (
        "Review these commits per the schema. Reply with json only.\n\n"
        + json.dumps({"commits": payload}, indent=2)
    )
    raw = call_llm(model=model, system=REVIEW_SYSTEM, user=user, token=token)
    # Extract first json object
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    text = re.sub(r"\s*```$", "", text)
    depth = 0
    start = None
    for i, c in enumerate(text):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                parsed = json.loads(text[start : i + 1])
                reviews = parsed.get("commits") or []
                # Attach URLs from input (LLM may not echo them)
                by_sha = {p["sha"]: p for p in payload}
                for r in reviews:
                    src = by_sha.get(r.get("sha"))
                    if src and "url" in src and "url" not in r:
                        r["url"] = src["url"]
                    if src and "short_sha" not in r:
                        r["short_sha"] = src.get("short_sha")
                return reviews
    raise ValueError("no JSON object in LLM response")


def aggregate(reviews: list[dict]) -> dict:
    """Roll the per-commit reviews into a finding-level summary."""
    by_kind: dict[str, int] = {}
    by_severity: dict[str, int] = {"none": 0, "low": 0, "medium": 0, "high": 0}
    areas: set[str] = set()
    high_or_medium: list[dict] = []
    for r in reviews:
        by_kind[r.get("kind", "other")] = by_kind.get(r.get("kind", "other"), 0) + 1
        sev = r.get("severity", "none")
        by_severity[sev] = by_severity.get(sev, 0) + 1
        for a in r.get("areas") or []:
            areas.add(a)
        if sev in ("medium", "high"):
            high_or_medium.append(r)
    return {
        "total_commits": len(reviews),
        "by_kind": by_kind,
        "by_severity": by_severity,
        "areas_touched": sorted(areas),
        "high_or_medium_commits": high_or_medium,
    }


def run(
    code_owner: str,
    code_repo: str,
    base_ref: str,
    head_ref: str,
    token: str,
    *,
    model: str = "openai/gpt-4o-mini",
    batch_size: int = 5,
    max_commits: int = 50,
) -> dict:
    try:
        commits = list_commits(code_owner, code_repo, base_ref, head_ref, token)
    except urllib.error.HTTPError as e:
        return {
            "module": "code_review",
            "ok": False,
            "error": f"HTTPError {e.code} listing commits {base_ref}...{head_ref}",
        }

    if len(commits) > max_commits:
        # Cap to avoid runaway; the very-large-release case is itself a signal
        commits = commits[:max_commits]
        capped = True
    else:
        capped = False

    reviews: list[dict] = []
    llm_used = False
    if token and commits:
        for i in range(0, len(commits), batch_size):
            batch = commits[i : i + batch_size]
            try:
                batch_reviews = review_batch(batch, model, token)
                reviews.extend(batch_reviews)
                llm_used = True
            except Exception as e:  # noqa: BLE001
                # On batch failure, fall back to heuristic for that batch
                for c in batch:
                    fb = heuristic_review(c)
                    fb["llm_error"] = str(e)[:200]
                    reviews.append(fb)
    elif commits:
        reviews = [heuristic_review(c) for c in commits]

    agg = aggregate(reviews)

    # Hard-flag any high-severity commit
    hard_flag = agg["by_severity"]["high"] > 0

    return {
        "module": "code_review",
        "ok": True,
        "code_owner": code_owner,
        "code_repo": code_repo,
        "base_ref": base_ref,
        "head_ref": head_ref,
        "llm_used": llm_used,
        "capped": capped,
        "reviews": reviews,
        "aggregate": agg,
        "hard_flag": hard_flag,
        "summary": (
            f"{agg['total_commits']} commits reviewed. "
            f"severity: {agg['by_severity']['high']}H "
            f"{agg['by_severity']['medium']}M "
            f"{agg['by_severity']['low']}L "
            f"{agg['by_severity']['none']}none. "
            f"areas: {', '.join(agg['areas_touched'][:6]) or '(none)'}"
        ),
    }


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("owner")
    ap.add_argument("repo")
    ap.add_argument("base")
    ap.add_argument("head")
    args = ap.parse_args()
    token = os.environ.get("GITHUB_TOKEN") or ""
    result = run(args.owner, args.repo, args.base, args.head, token)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
