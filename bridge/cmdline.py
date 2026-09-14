"""How the bridge calls the Claude CLI. Kept out of hh_scout_bridge.py so the project's own venv can test it
without FastAPI: the bridge has its own venv, the tests do not.
"""

from __future__ import annotations


def build_cmd(claude_bin: str, system_text: str, model: str, *, allow_web: bool = False,
              max_turns: int = 1) -> list[str]:
    """The CLI command line.

    Tool-less and single-turn by default: triage, evaluation and letters must be reproducible and cheap, and
    extra turns without tools only cost money. `allow_web` is for company research alone — it grants the two
    read-only web tools and nothing else, so `bypassPermissions` cannot reach files or the shell, and the cwd
    is already an empty sandbox (BRIDGE_WORKDIR, no CLAUDE.md).
    """
    cmd = [claude_bin, "-p", "--output-format", "json",
           "--max-turns", str(max_turns if allow_web else 1),
           "--tools", "WebFetch,WebSearch" if allow_web else "",
           "--model", model]
    if allow_web:
        cmd += ["--permission-mode", "bypassPermissions"]
    if system_text:
        cmd += ["--system-prompt", system_text]
    return cmd
