"""The rule that matters: nothing reached without the model can say "clean"."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "judge"))

import judge as judge_mod  # noqa: E402
import llm  # noqa: E402

PROMPT = ROOT / "judge" / "prompt.yml"

QUIET = [
    {"module": "committer_diff", "ok": True, "hard_flag": False, "summary": "0 new authors"},
    {"module": "code_review", "ok": True, "hard_flag": False, "llm_used": True, "reviews": [{"sha": "a"}]},
    {"module": "url_inventory", "ok": True, "hard_flag": False, "summary": "0 new hosts"},
]


def run(findings, monkeypatch, reply=None, error=None):
    def fake_call(**_):
        if error:
            raise llm.LLMUnavailable(error)
        return reply

    monkeypatch.setattr(llm, "call", fake_call)
    return judge_mod.judge(
        trigger_owner="o", trigger_repo="r", new_tag="v2", baseline_tag="v1",
        release_notes="notes", findings=findings, prompt_path=PROMPT,
    )


CLEAN_REPLY = '{"verdict": "clean", "score": 9, "headline": "fine", "anomalies": [], "consistent_with_notes": true, "reasoning": "ok"}'


def test_model_says_clean_on_quiet_findings(monkeypatch):
    v = run(QUIET, monkeypatch, reply=CLEAN_REPLY)
    assert v["verdict"] == "clean"
    assert v["judge_source"] == "claude:sonnet"


def test_model_unreachable_is_never_clean(monkeypatch):
    v = run(QUIET, monkeypatch, error="claude returned an error: 401")
    assert v["verdict"] == "review"
    assert v["headline"].startswith("Unjudged")
    assert "401" in v["judge_error"]


def test_fake_judge_is_never_clean(monkeypatch):
    monkeypatch.setenv("USE_FAKE_JUDGE", "1")
    v = run(QUIET, monkeypatch, reply=CLEAN_REPLY)
    assert v["verdict"] == "review"


def test_hard_flag_overrides_a_clean_model(monkeypatch):
    findings = [dict(QUIET[0], hard_flag=True, summary="1 new author")] + QUIET[1:]
    v = run(findings, monkeypatch, reply=CLEAN_REPLY)
    assert v["verdict"] == "review"
    assert v["hard_flagged_modules"] == ["committer_diff"]


def test_code_review_without_model_overrides_a_clean_model(monkeypatch):
    findings = [QUIET[0], dict(QUIET[1], llm_used=False), QUIET[2]]
    v = run(findings, monkeypatch, reply=CLEAN_REPLY)
    assert v["verdict"] == "review"
    assert v["degraded_modules"] == ["code_review"]


def test_module_error_overrides_a_clean_model(monkeypatch):
    findings = QUIET[:2] + [{"module": "url_inventory", "ok": False, "summary": "no asset"}]
    v = run(findings, monkeypatch, reply=CLEAN_REPLY)
    assert v["verdict"] == "review"
    assert "url_inventory" in v["degraded_modules"]


def test_unparseable_reply_is_never_clean(monkeypatch):
    v = run(QUIET, monkeypatch, reply="I think it looks fine!")
    assert v["verdict"] == "review"


def test_call_without_cli_raises(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: None)
    try:
        llm.call(system="s", user="u")
    except llm.LLMUnavailable as e:
        assert "not found" in str(e)
    else:
        raise AssertionError("expected LLMUnavailable")
