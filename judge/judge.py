"""
judge — render a structured verdict for a release given module findings.

Asks Claude through the `claude` CLI (see llm.py). GitHub Models, the
original backend, was retired on 2026-07-30.

A verdict of "clean" means the model judged the release and nothing tripped.
Anything reached without the model (USE_FAKE_JUDGE=1, the CLI missing, the
call failing) comes out as "review" with an "unjudged" headline. The fallback
used to say "clean" when no module hard-flagged, and after GitHub Models went
away that stamped three Apollo releases clean that nobody had judged.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm  # noqa: E402


def load_prompt(path: Path) -> dict:
    """Load the prompt config from YAML.

    Uses PyYAML if available (default on Ubuntu Actions runners), falls back
    to a line-based block-scalar parser for environments without PyYAML.
    The fallback supports blank lines inside block scalars — a real YAML
    block ends at a less-indented non-blank line, NOT at the first blank.
    """
    text = path.read_text()
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError:
        pass

    out: dict[str, object] = {}
    # Simple scalars
    for key, caster in (
        ("model", str),
        ("temperature", float),
        ("max_tokens", int),
        ("version", int),
    ):
        m = re.search(rf"^{key}:\s*(\S+)", text, re.M)
        if m:
            out[key] = caster(m.group(1))

    # Block scalars (`key: |` then indented content until the next top-level key)
    for key in ("system", "user_template"):
        start = re.search(rf"^{key}:\s*\|\s*\n", text, re.M)
        if not start:
            raise ValueError(f"missing block {key}")
        rest = text[start.end() :]
        lines = rest.split("\n")
        block_lines: list[str] = []
        for line in lines:
            if line == "" or line.startswith(" ") or line.startswith("\t"):
                block_lines.append(line)
            else:
                break
        indents = [len(l) - len(l.lstrip()) for l in block_lines if l.strip()]
        indent = min(indents) if indents else 0
        out[key] = "\n".join(
            (l[indent:] if len(l) >= indent else l) for l in block_lines
        ).rstrip()
    return out


def render(template: str, vars: dict[str, str]) -> str:
    for k, v in vars.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def compact_findings_for_judge(findings: list[dict], char_budget: int) -> list[dict]:
    """Project findings into a small, judge-ready shape that fits a token budget.

    The request has a token budget (see judge()). The code_review
    module's per-commit reviews dominate request size on large releases and
    can overflow it, forcing a fallback. Keep security-relevant
    commits (high/medium) in full and roll routine ones (low/none) into compact
    one-liners, then trim to `char_budget` — dropping routine detail first, then
    lowest-severity notable detail — recording how much was omitted so the judge
    knows its view is partial. Other (small) modules pass through unchanged.
    """
    SEV_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}
    out: list[dict] = []
    cr_block: dict | None = None
    for f in findings:
        if f.get("module") != "code_review":
            out.append(f)
            continue
        reviews = f.get("reviews") or []
        notable: list[dict] = []
        routine: list[dict] = []
        for r in sorted(
            reviews, key=lambda x: -SEV_RANK.get(x.get("severity", "none"), 0)
        ):
            sev = r.get("severity", "none")
            sha = r.get("short_sha") or (r.get("sha") or "")[:8]
            title = (r.get("title") or "").replace("\n", " ").strip()[:120]
            if sev in ("high", "medium"):
                notable.append(
                    {
                        "sha": sha,
                        "author": r.get("author"),
                        "severity": sev,
                        "kind": r.get("kind"),
                        "areas": r.get("areas"),
                        "title": title,
                        "summary": (r.get("summary") or "").strip()[:400],
                        "security": (r.get("security_implications") or "").strip()[
                            :300
                        ],
                        "privacy": (r.get("privacy_implications") or "").strip()[:300],
                        "safety": (r.get("safety_implications") or "").strip()[:300],
                    }
                )
            else:
                routine.append({"sha": sha, "severity": sev, "title": title})
        cr_block = {
            "module": "code_review",
            "ok": f.get("ok"),
            "hard_flag": f.get("hard_flag"),
            "summary": f.get("summary"),
            "commit_count": len(reviews),
            "notable_commits": notable,
            "routine_commits": routine,
        }
        out.append(cr_block)

    if cr_block is not None:
        # Measure with the SAME serialization the request uses (indent=2 adds
        # ~25% over compact json) so the budget reflects real payload size.
        size = lambda: len(json.dumps(out, indent=2))  # noqa: E731
        omitted_routine = 0
        while size() > char_budget and cr_block["routine_commits"]:
            cr_block["routine_commits"].pop()
            omitted_routine += 1
        if omitted_routine:
            cr_block["routine_commits_omitted"] = omitted_routine
        omitted_notable = 0
        # notable is severity-sorted high→low, so popping the tail sheds the
        # least-important commits first.
        while size() > char_budget and cr_block["notable_commits"]:
            cr_block["notable_commits"].pop()
            omitted_notable += 1
        if omitted_notable:
            cr_block["notable_commits_omitted"] = omitted_notable
    return out


def heuristic_verdict(findings: list[dict], why: str) -> dict:
    """Verdict without the model. Never "clean": nobody judged the release.

    `why` names the reason the model did not run, and goes in the headline so
    the email and the issue say it too, not only the reasoning text.
    """
    hard = [f for f in findings if f.get("hard_flag")]
    reasoning = (
        f"⚠️ The LLM judge did not run ({why}). Only the programmatic checks ran. "
        "This release is unjudged, so it stays open for a human, whatever the checks found."
    )
    if hard:
        return {
            "verdict": "review",
            "decision_inputs": [
                f"Review {f.get('module', '?')} finding: {f.get('summary', '(no summary)')}"
                for f in hard
            ],
            "score": 5,
            "headline": f"Unjudged, and a check tripped: {', '.join(str(f.get('module', '?')) for f in hard)}",
            "anomalies": [
                {
                    "severity": "medium",
                    "module": f.get("module", "?"),
                    "what": f.get("summary", "hard flag set"),
                    "why_it_matters": "Hard flag indicates a programmatic check tripped.",
                }
                for f in hard
            ],
            "consistent_with_notes": False,
            "reasoning": reasoning,
        }
    return {
        "verdict": "review",
        "decision_inputs": ["The checks found nothing, but no model read the diff. Look at the compare link."],
        "score": 6,
        "headline": "Unjudged: the LLM judge did not run",
        "anomalies": [],
        "consistent_with_notes": False,
        "reasoning": reasoning,
    }


def degraded_modules(findings: list[dict]) -> list[str]:
    """Modules that ran without the part that does the real checking."""
    out = []
    for f in findings:
        name = str(f.get("module", "?"))
        if f.get("ok") is False:
            out.append(name)
        elif name == "code_review" and f.get("reviews") and not f.get("llm_used"):
            out.append(name)
    return out


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM response."""
    # Strip code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    # Find first { ... } block
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
                return json.loads(text[start : i + 1])
    raise ValueError("no JSON object found in LLM response")


def judge(
    *,
    trigger_owner: str,
    trigger_repo: str,
    new_tag: str,
    baseline_tag: str,
    release_notes: str,
    findings: list[dict],
    prompt_path: Path,
    code_compare_url: str = "",
    release_compare_url: str = "",
    release_url: str = "",
) -> dict:
    """Render a verdict. Returns a dict with `verdict`, `score`, etc.

    Asks Claude; without it the verdict is "review" (unjudged). Always applies
    the rule-based overrides: a hard flag from any module, or a module that
    could not do its job, means the verdict cannot be `clean`, whatever the
    model says.

    Compare URLs are passed into the prompt so the LLM can include them in
    its `decision_inputs` for the human reviewer.
    """
    if os.environ.get("USE_FAKE_JUDGE") == "1":
        result = heuristic_verdict(findings, "USE_FAKE_JUDGE=1")
        result["judge_source"] = "heuristic"
    else:
        prompt = load_prompt(prompt_path)
        # Size the findings payload to what's left after the system prompt +
        # notes, rather than a fixed char cap that truncates JSON mid-structure.
        # ~4 chars/token. Claude's window is far larger than GitHub Models'
        # old 8000-token cap; 60K keeps a big release affordable.
        CHARS_PER_TOKEN = 4
        REQUEST_TOKEN_LIMIT = 60000
        SAFETY_TOKENS = 700
        notes = release_notes[:3000]
        overhead_tokens = (
            len(prompt["system"]) + len(prompt["user_template"]) + len(notes)
        ) // CHARS_PER_TOKEN
        findings_budget_chars = max(
            2000,
            (REQUEST_TOKEN_LIMIT - overhead_tokens - SAFETY_TOKENS) * CHARS_PER_TOKEN,
        )
        compact = compact_findings_for_judge(findings, findings_budget_chars)
        user = render(
            prompt["user_template"],
            {
                "TRIGGER_OWNER": trigger_owner,
                "TRIGGER_REPO": trigger_repo,
                "NEW_TAG": new_tag,
                "BASELINE_TAG": baseline_tag,
                "RELEASE_NOTES": notes,
                "MODULE_FINDINGS_JSON": json.dumps(compact, indent=2),
                "CODE_COMPARE_URL": code_compare_url or "(not available)",
                "RELEASE_COMPARE_URL": release_compare_url or "(not available)",
                "RELEASE_URL": release_url or "(not available)",
            },
        )
        raw = None
        try:
            raw = llm.call(system=prompt["system"], user=user, model=prompt["model"])
            result = extract_json(raw)
            result["judge_source"] = f"claude:{prompt['model']}"
        except llm.LLMUnavailable as e:
            result = heuristic_verdict(findings, str(e)[:200])
            result["judge_source"] = "heuristic-fallback:LLMUnavailable"
            result["judge_error"] = str(e)[:300]
        except (ValueError, KeyError) as e:
            # LLM returned content we couldn't parse. Don't silently fall back
            # to clean — treat as a low-confidence review and surface the raw
            # response so the prompt can be tuned.
            result = {
                "verdict": "review",
                "score": 4,
                "headline": "Could not parse LLM verdict; manual review",
                "anomalies": [],
                "consistent_with_notes": False,
                "reasoning": (
                    f"LLM returned a response that couldn't be parsed as the "
                    f"expected JSON schema ({type(e).__name__}: {e}). Raw "
                    f"response preserved below in `raw_response` for prompt "
                    f"tuning. Falling back to 'review' so we don't claim "
                    f"'clean' on a parse failure."
                ),
                "judge_source": f"unparseable:{type(e).__name__}",
                "judge_error": str(e)[:300],
            }
        if raw is not None:
            result["raw_response"] = raw[:4000]

    # Rule-based overrides. A hard flag means not clean. So does a module that
    # could not do its job: code_review falling back to its heuristic because
    # the model was unreachable is "we didn't read the commits", not "the
    # commits are fine".
    hard_flagged = [f.get("module") for f in findings if f.get("hard_flag")]
    degraded = degraded_modules(findings)
    if (hard_flagged or degraded) and result.get("verdict") == "clean":
        why = []
        if hard_flagged:
            why.append("hard flag from " + ", ".join(m for m in hard_flagged if m))
        if degraded:
            why.append("could not run fully: " + ", ".join(degraded))
        result["verdict"] = "review"
        result["score"] = min(result.get("score", 5), 5)
        result["reasoning"] = "Override: " + "; ".join(why) + ". " + result.get("reasoning", "")

    result["hard_flagged_modules"] = hard_flagged
    result["degraded_modules"] = degraded
    return result


def main() -> int:
    """CLI: read findings from stdin (JSON list), print verdict (JSON)."""
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--new-tag", required=True)
    ap.add_argument("--baseline-tag", required=True)
    ap.add_argument("--notes-file", required=True)
    ap.add_argument("--findings-file", required=True)
    ap.add_argument("--prompt", default=str(Path(__file__).parent / "prompt.yml"))
    args = ap.parse_args()

    notes = Path(args.notes_file).read_text()
    findings = json.loads(Path(args.findings_file).read_text())

    result = judge(
        trigger_owner=args.owner,
        trigger_repo=args.repo,
        new_tag=args.new_tag,
        baseline_tag=args.baseline_tag,
        release_notes=notes,
        findings=findings,
        prompt_path=Path(args.prompt),
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
