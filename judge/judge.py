"""
judge — render a structured verdict for a release given module findings.

Talks to GitHub Models via the OpenAI-compatible inference endpoint, using
the workflow's built-in GITHUB_TOKEN (no separate API key needed).

If GITHUB_TOKEN is absent or USE_FAKE_JUDGE=1 in env, returns a heuristic
verdict — useful for offline local testing.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

MODELS_URL = "https://models.github.ai/inference/chat/completions"


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

    GitHub Models caps gpt-4o-mini requests at 8000 tokens. The code_review
    module's per-commit reviews dominate request size on large releases and
    will 413 the request, forcing a heuristic fallback. Keep security-relevant
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


def heuristic_verdict(findings: list[dict]) -> dict:
    """Offline fallback. Conservative: any hard_flag → review (never blocked)."""
    hard = [f for f in findings if f.get("hard_flag")]
    if hard:
        return {
            "verdict": "review",
            "decision_inputs": [
                f"Review {f.get('module', '?')} finding: {f.get('summary', '(no summary)')}"
                for f in hard
            ],
            "score": 5,
            "headline": "Hard flag from module(s); manual review needed",
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
            "reasoning": "Heuristic judge (no LLM available). One or more modules raised hard flags.",
        }
    return {
        "verdict": "clean",
        "score": 8,
        "headline": "No hard flags from any module",
        "anomalies": [],
        "consistent_with_notes": True,
        "reasoning": "Heuristic judge (no LLM available). No module flagged anomalies.",
    }


def call_github_models(
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    system: str,
    user: str,
    token: str,
) -> str:
    body = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # Force structured JSON output — without this, GPT-4o tends to wrap
        # the JSON in markdown or precede it with prose despite system-prompt
        # instructions. OpenAI's json_object mode is a hard constraint.
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
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Surface the response body so we can debug 4xx/5xx
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            err_body = "(no body)"
        raise urllib.error.HTTPError(
            e.url, e.code, f"{e.reason} :: {err_body}", e.headers, None
        ) from e
    return data["choices"][0]["message"]["content"]


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

    Uses GitHub Models if GITHUB_TOKEN is set; falls back to heuristic
    otherwise. Always applies the rule-based override: if any module has
    `hard_flag: true`, the verdict cannot be `clean` regardless of what the
    LLM says.

    Compare URLs are passed into the prompt so the LLM can include them in
    its `decision_inputs` for the human reviewer.
    """
    token = os.environ.get("GITHUB_TOKEN")
    use_fake = os.environ.get("USE_FAKE_JUDGE") == "1"

    if not token or use_fake:
        result = heuristic_verdict(findings)
        result["judge_source"] = "heuristic"
    else:
        prompt = load_prompt(prompt_path)
        # GitHub Models caps gpt-4o-mini requests at 8000 tokens. Size the
        # findings payload to what's left after the system prompt + notes,
        # rather than a fixed char cap that 413s on large releases (and that
        # truncated JSON mid-structure). ~4 chars/token, with a safety margin.
        CHARS_PER_TOKEN = 4
        REQUEST_TOKEN_LIMIT = 8000
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
            raw = call_github_models(
                model=prompt["model"],
                temperature=prompt["temperature"],
                max_tokens=prompt["max_tokens"],
                system=prompt["system"],
                user=user,
                token=token,
            )
            result = extract_json(raw)
            result["judge_source"] = f"github-models:{prompt['model']}"
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            result = heuristic_verdict(findings)
            result["judge_source"] = f"heuristic-fallback:{type(e).__name__}"
            result["judge_error"] = str(e)[:300]
            # Don't leave the misleading "no LLM available" text from
            # heuristic_verdict() — the LLM *was* reachable, the call failed.
            # Name the real cause so a degraded verdict is self-explanatory.
            result["reasoning"] = (
                "⚠️ The LLM judge did not run — the GitHub Models request "
                f"failed ({str(e)[:200]}). Fell back to a conservative "
                "heuristic: any module hard_flag forces 'review'. Treat this "
                "as 'unjudged — a human should look', not a real LLM verdict."
            )
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

    # Rule-based override: hard_flag => not clean
    hard_flagged = [f.get("module") for f in findings if f.get("hard_flag")]
    if hard_flagged and result.get("verdict") == "clean":
        result["verdict"] = "review"
        result["score"] = min(result.get("score", 5), 5)
        result["reasoning"] = (
            "Override: hard flag from module(s) "
            + ", ".join(m for m in hard_flagged if m)
            + ". "
            + result.get("reasoning", "")
        )

    result["hard_flagged_modules"] = hard_flagged
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
