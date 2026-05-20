"""
committer-diff — Compare authors between two refs of a GitHub repo.

Surfaces:
  - new_authors: logins committing for the first time in this window
  - returning_authors: logins seen in prior history but not in immediate previous release
  - existing_authors: logins active in both releases

A brand-new author landing in a release is a strong supply-chain signal — not
always bad (legitimate first contributions happen) but always worth surfacing.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error

GH_API = "https://api.github.com"


def gh_request(path: str, token: str | None = None) -> dict | list:
    url = f"{GH_API}{path}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def list_commits_between(
    owner: str, repo: str, base: str, head: str, token: str | None
) -> list[dict]:
    """Use GitHub's compare endpoint — returns commits in head not in base."""
    data = gh_request(f"/repos/{owner}/{repo}/compare/{base}...{head}", token)
    return data.get("commits", [])


def list_authors_lifetime(
    owner: str, repo: str, until_sha: str, token: str | None, page_cap: int = 10
) -> set[str]:
    """List unique committer logins from beginning of history up to a ref.
    Page-capped to avoid scanning the full history for very old repos."""
    seen: set[str] = set()
    page = 1
    while page <= page_cap:
        commits = gh_request(
            f"/repos/{owner}/{repo}/commits?sha={until_sha}&per_page=100&page={page}",
            token,
        )
        if not commits:
            break
        for c in commits:
            if c.get("author") and c["author"].get("login"):
                seen.add(c["author"]["login"])
        if len(commits) < 100:
            break
        page += 1
    return seen


def run(
    owner: str, repo: str, base_ref: str, head_ref: str, token: str | None = None
) -> dict:
    """Run the module.

    Returns a structured finding dict suitable for the judge.
    """
    try:
        commits = list_commits_between(owner, repo, base_ref, head_ref, token)
    except urllib.error.HTTPError as e:
        return {
            "module": "committer_diff",
            "ok": False,
            "error": f"HTTPError {e.code} fetching compare {base_ref}...{head_ref}",
        }

    window_authors: dict[str, int] = {}
    for c in commits:
        login = (c.get("author") or {}).get("login")
        if not login:
            # commits with unknown author (email not linked to a GH account)
            login = "<unknown>"
        window_authors[login] = window_authors.get(login, 0) + 1

    # Lifetime authors up to (but not including) this window
    try:
        prior_authors = list_authors_lifetime(owner, repo, base_ref, token)
    except urllib.error.HTTPError as e:
        return {
            "module": "committer_diff",
            "ok": False,
            "error": f"HTTPError {e.code} fetching lifetime authors",
        }

    new_authors = sorted(set(window_authors) - prior_authors - {"<unknown>"})
    existing_authors = sorted(set(window_authors) & prior_authors)
    has_unknown = "<unknown>" in window_authors

    hard_flag = bool(new_authors)  # any first-time committer is a hard flag

    return {
        "module": "committer_diff",
        "ok": True,
        "owner": owner,
        "repo": repo,
        "base_ref": base_ref,
        "head_ref": head_ref,
        "window_commit_count": len(commits),
        "window_authors": window_authors,
        "new_authors": new_authors,
        "existing_authors": existing_authors,
        "has_unknown_author_commits": has_unknown,
        "hard_flag": hard_flag,
        "summary": (
            f"{len(commits)} commits by {len(window_authors)} authors. "
            f"{len(new_authors)} new author(s): {new_authors or 'none'}. "
            + ("Includes commits with unknown author. " if has_unknown else "")
        ),
    }


def main() -> int:
    """CLI entrypoint for local testing.

    Usage: python committer_diff.py OWNER REPO BASE_REF HEAD_REF
    """
    if len(sys.argv) != 5:
        print("usage: committer_diff.py OWNER REPO BASE_REF HEAD_REF", file=sys.stderr)
        return 2
    owner, repo, base, head = sys.argv[1:5]
    token = os.environ.get("GITHUB_TOKEN")
    result = run(owner, repo, base, head, token)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
