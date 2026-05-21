"""
poll_and_assay — End-to-end runner.

Combines: poll for new release → run modules → call judge → write verdict
issue. Designed to run inside a GitHub Actions job, but can run locally
with GITHUB_TOKEN set.

Usage as GitHub Action step:
    python scripts/poll_and_assay.py \\
        --assay-owner epheterson --assay-repo assay \\
        --tracked-owner Balackburn --tracked-repo Apollo \\
        --code-owner JeffreyCA --code-repo Apollo-ImprovedCustomApi \\
        --code-tag-prefix v \\
        --binary-asset-pattern 'GLASS_Apollo-.*\\.ipa$' \\
        --binary-inner-path 'Payload/Apollo.app/Apollo' \\
        --allowlist examples/apollo/allowlist.json

Outputs (also written as GITHUB_OUTPUT lines when in Actions):
    new_release  = true|false
    verdict      = clean|review|blocked|baseline|none
    tag          = the release tag (or empty)
    issue_url    = URL of the verdict/baseline issue (or empty)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Ensure we can import modules/ and judge/ and scripts/ regardless of CWD
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "modules"))
sys.path.insert(0, str(ROOT / "judge"))
sys.path.insert(0, str(ROOT / "scripts"))

import committer_diff  # noqa: E402
import url_inventory  # noqa: E402
import judge as judge_mod  # noqa: E402
import state_via_issues as state_mod  # noqa: E402

GH_API = "https://api.github.com"


def gh_get(path: str, token: str) -> dict | list:
    req = urllib.request.Request(f"{GH_API}{path}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def latest_release(owner: str, repo: str, token: str) -> dict:
    return gh_get(f"/repos/{owner}/{repo}/releases/latest", token)


def download_asset(asset_url: str, token: str, dest: Path) -> Path:
    """Download a release asset (requires Accept: application/octet-stream)."""
    req = urllib.request.Request(asset_url)
    req.add_header("Accept", "application/octet-stream")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=300) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def extract_inner(ipa_path: Path, inner_path: str, dest_dir: Path) -> Path:
    """Extract a single inner file from a zip (IPA is a zip)."""
    with zipfile.ZipFile(ipa_path) as z:
        # Some IPAs may have the binary at a slightly different path on case
        # differences; do a case-insensitive search if exact name missing.
        names = z.namelist()
        if inner_path in names:
            target = inner_path
        else:
            target = next((n for n in names if n.lower() == inner_path.lower()), None)
        if not target:
            raise FileNotFoundError(f"{inner_path} not in archive")
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_path = dest_dir / Path(target).name
        with z.open(target) as src, out_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        return out_path


def code_tag_from_release_notes(notes: str) -> str | None:
    """For Apollo: notes start with 'ImprovedCustomApi version: `vX.Y.Z`'."""
    m = re.search(r"ImprovedCustomApi version:\s*`?v?([\d.]+)`?", notes)
    return f"v{m.group(1)}" if m else None


def emit_output(key: str, value: str) -> None:
    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path:
        # Use multiline-safe heredoc for long values
        with open(out_path, "a") as f:
            f.write(f"{key}<<__EOF__\n{value}\n__EOF__\n")
    print(f"::output {key}={value[:200]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assay-owner", required=True)
    ap.add_argument("--assay-repo", required=True)
    ap.add_argument(
        "--tracked-owner",
        required=True,
        help="Repo that publishes the releases we watch.",
    )
    ap.add_argument("--tracked-repo", required=True)
    ap.add_argument(
        "--code-owner",
        default=None,
        help="Repo where the actual source lives (defaults to tracked).",
    )
    ap.add_argument("--code-repo", default=None)
    ap.add_argument(
        "--binary-asset-pattern",
        default=None,
        help="Regex matching the release asset name to download for binary diff.",
    )
    ap.add_argument(
        "--binary-inner-path",
        default=None,
        help="Path to the binary inside the archive (e.g. Payload/Apollo.app/Apollo).",
    )
    ap.add_argument("--allowlist", default=None, help="Path to host allowlist JSON.")
    ap.add_argument(
        "--baseline-binary-cache",
        default="/tmp/assay-baseline",
        help="Where to keep the previous release's binary across runs.",
    )
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN required", file=sys.stderr)
        return 1

    code_owner = args.code_owner or args.tracked_owner
    code_repo = args.code_repo or args.tracked_repo

    # ── Poll
    rel = latest_release(args.tracked_owner, args.tracked_repo, token)
    tag = rel["tag_name"]
    notes = rel.get("body") or ""
    release_url = rel["html_url"]

    last_state = state_mod.get_last_state(
        args.assay_owner, args.assay_repo, args.tracked_owner, args.tracked_repo, token
    )

    if not last_state:
        # Bootstrap: write a baseline issue and exit
        issue = state_mod.write_baseline_issue(
            assay_owner=args.assay_owner,
            assay_repo=args.assay_repo,
            tracked_owner=args.tracked_owner,
            tracked_repo=args.tracked_repo,
            tag=tag,
            release_url=release_url,
            token=token,
        )
        emit_output("new_release", "false")
        emit_output("verdict", "baseline")
        emit_output("tag", tag)
        emit_output("issue_url", issue["html_url"])
        print(f"baseline established at {tag}")
        return 0

    if last_state.get("tag") == tag:
        # No new release
        emit_output("new_release", "false")
        emit_output("verdict", "none")
        emit_output("tag", tag)
        emit_output("issue_url", "")
        print(f"no new release; latest still {tag}")
        return 0

    baseline_tag = last_state["tag"]
    print(f"new release detected: {baseline_tag} → {tag}")

    # ── Run modules
    findings: list[dict] = []

    # committer_diff against the code repo (source of truth for logic).
    # The tracked repo (e.g. Balackburn/Apollo) and the code repo (e.g.
    # JeffreyCA/Apollo-ImprovedCustomApi) often use different tag formats.
    # Derive the code tags by parsing each release's notes.
    code_head = code_tag_from_release_notes(notes) or tag
    code_baseline = None
    try:
        baseline_rel = gh_get(
            f"/repos/{args.tracked_owner}/{args.tracked_repo}/releases/tags/{baseline_tag}",
            token,
        )
        code_baseline = code_tag_from_release_notes(baseline_rel.get("body") or "")
    except urllib.error.HTTPError as e:
        print(f"could not fetch baseline release {baseline_tag}: {e}", file=sys.stderr)
    code_baseline = code_baseline or baseline_tag  # last-resort fallback

    cd = committer_diff.run(code_owner, code_repo, code_baseline, code_head, token)
    # A module that errored out should hard-flag — "we couldn't audit X" is
    # not the same as "X is clean."
    if not cd.get("ok"):
        cd["hard_flag"] = True
    findings.append(cd)

    # url_inventory against the binary asset (if configured)
    if args.binary_asset_pattern and args.binary_inner_path:
        asset = next(
            (
                a
                for a in rel.get("assets", [])
                if re.search(args.binary_asset_pattern, a["name"])
            ),
            None,
        )
        if asset:
            with tempfile.TemporaryDirectory() as td:
                tdir = Path(td)
                ipa_path = tdir / asset["name"]
                print(f"downloading {asset['name']} ({asset['size']//1024//1024} MB) …")
                download_asset(asset["url"], token, ipa_path)
                new_bin = extract_inner(ipa_path, args.binary_inner_path, tdir / "new")

                baseline_cache = Path(args.baseline_binary_cache)
                baseline_cache.mkdir(parents=True, exist_ok=True)
                baseline_key = (
                    f"{args.tracked_owner}-{args.tracked_repo}-{baseline_tag}".replace(
                        "/", "_"
                    )
                )
                baseline_bin = baseline_cache / baseline_key

                ui = url_inventory.run(
                    new_path=new_bin,
                    baseline_path=baseline_bin if baseline_bin.exists() else None,
                    allowlist_path=Path(args.allowlist) if args.allowlist else None,
                )
                findings.append(ui)

                # Update baseline cache for next run
                shutil.copyfile(
                    new_bin,
                    baseline_cache
                    / f"{args.tracked_owner}-{args.tracked_repo}-{tag}".replace(
                        "/", "_"
                    ),
                )
        else:
            findings.append(
                {
                    "module": "url_inventory",
                    "ok": False,
                    "summary": f"no asset matched pattern {args.binary_asset_pattern!r}",
                }
            )

    # ── Compute compare URLs (handed to judge + included in verdict issue)
    code_compare_url = (
        f"https://github.com/{code_owner}/{code_repo}/compare/{code_baseline}...{code_head}"
        if code_baseline and code_head and code_baseline != code_head
        else ""
    )
    release_compare_url = f"https://github.com/{args.tracked_owner}/{args.tracked_repo}/compare/{baseline_tag}...{tag}"

    # ── Judge
    verdict = judge_mod.judge(
        trigger_owner=args.tracked_owner,
        trigger_repo=args.tracked_repo,
        new_tag=tag,
        baseline_tag=baseline_tag,
        release_notes=notes,
        findings=findings,
        prompt_path=ROOT / "judge" / "prompt.yml",
        code_compare_url=code_compare_url,
        release_compare_url=release_compare_url,
        release_url=release_url,
    )
    # Attach links to the verdict so the issue + ledger can render them
    verdict["links"] = {
        "code_compare_url": code_compare_url,
        "release_compare_url": release_compare_url,
        "release_url": release_url,
    }

    # ── Write verdict issue
    issue = state_mod.write_verdict_issue(
        assay_owner=args.assay_owner,
        assay_repo=args.assay_repo,
        tracked_owner=args.tracked_owner,
        tracked_repo=args.tracked_repo,
        tag=tag,
        baseline_tag=baseline_tag,
        verdict=verdict,
        release_url=release_url,
        findings=findings,
        token=token,
    )

    # Persist verdict to repo for archival (verdicts/{tag}.json)
    verdicts_dir = ROOT / "verdicts"
    verdicts_dir.mkdir(exist_ok=True)
    safe_tag = re.sub(r"[^A-Za-z0-9._-]", "_", tag)
    (
        verdicts_dir / f"{args.tracked_owner}-{args.tracked_repo}-{safe_tag}.json"
    ).write_text(
        json.dumps(
            {
                "verdict": verdict,
                "findings": findings,
                "tag": tag,
                "baseline": baseline_tag,
            },
            indent=2,
        )
    )

    # ── Update LEDGER.md (rebuilt from all verdict files so it stays consistent)
    try:
        import update_ledger

        update_ledger.main()
    except Exception as e:  # noqa: BLE001
        print(f"warning: failed to update LEDGER.md: {e}", file=sys.stderr)

    emit_output("new_release", "true")
    emit_output("verdict", verdict.get("verdict", "unknown"))
    emit_output("tag", tag)
    emit_output("issue_url", issue["html_url"])
    emit_output("headline", verdict.get("headline", ""))
    emit_output("score", str(verdict.get("score", "")))
    emit_output("reasoning", verdict.get("reasoning", ""))
    print(f"\nverdict: {verdict.get('verdict')} (score {verdict.get('score')})")
    print(f"issue: {issue['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
