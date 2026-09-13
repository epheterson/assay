"""
llm — one model call through Claude Code in headless mode.

assay used GitHub Models until GitHub retired it on 2026-07-30. This calls
the `claude` CLI instead, so it runs on a Claude subscription: in Actions the
token comes from the CLAUDE_CODE_OAUTH_TOKEN secret (made with
`claude setup-token`); on a laptop it uses whatever `claude` is logged in as.

The call is locked down to a single text answer: no tools, no settings files,
no MCP servers, no session saved. The prompt goes in on stdin so a large
findings payload never hits the argument-length limit.

Any failure raises LLMUnavailable. Callers must treat that as "not judged",
never as "nothing found".
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile

DEFAULT_MODEL = "sonnet"
TIMEOUT_S = 300


class LLMUnavailable(RuntimeError):
    """The model could not be asked, or did not answer."""


def call(
    *, system: str, user: str, model: str = DEFAULT_MODEL, timeout: int = TIMEOUT_S
) -> str:
    """Return the model's text reply. Raises LLMUnavailable on any failure."""
    exe = shutil.which("claude")
    if not exe:
        raise LLMUnavailable("claude CLI not found on PATH")
    cmd = [
        exe,
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--system-prompt",
        system,
        "--tools",
        "",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--disable-slash-commands",
    ]
    try:
        # Run from an empty directory so no CLAUDE.md or project config is picked up.
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run(
                cmd, input=user, capture_output=True, text=True, timeout=timeout, cwd=td
            )
    except subprocess.TimeoutExpired as e:
        raise LLMUnavailable(f"claude timed out after {timeout}s") from e
    except OSError as e:
        raise LLMUnavailable(f"could not start claude: {e}") from e

    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise LLMUnavailable(
            f"claude exited {proc.returncode} without a JSON result: {detail}"
        )

    if proc.returncode != 0 or out.get("is_error") or out.get("subtype") != "success":
        detail = str(
            out.get("result") or out.get("api_error_status") or proc.stderr
        ).strip()[:300]
        raise LLMUnavailable(
            f"claude returned an error (exit {proc.returncode}, {out.get('subtype')}): {detail}"
        )

    text = out.get("result")
    if not isinstance(text, str) or not text.strip():
        raise LLMUnavailable("claude returned an empty result")
    return text
