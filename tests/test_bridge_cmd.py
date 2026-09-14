"""The bridge's CLI command line (bridge/cmdline.py).

Triage, evaluation and letters must stay tool-less and single-turn — that is what makes them reproducible and
cheap. Only company research may read the web, and only the two read-only web tools, never files or shell.
"""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "bridge_cmdline", Path(__file__).resolve().parents[1] / "bridge" / "cmdline.py")
cmdline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cmdline)


def test_default_call_has_no_tools_and_one_turn():
    cmd = cmdline.build_cmd("claude", "система", "opus")
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--max-turns") + 1] == "1"
    assert "--permission-mode" not in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "система"


def test_research_call_gets_the_two_web_tools_and_nothing_else():
    cmd = cmdline.build_cmd("claude", "", "sonnet", allow_web=True, max_turns=6)
    assert cmd[cmd.index("--tools") + 1] == "WebFetch,WebSearch"
    assert cmd[cmd.index("--max-turns") + 1] == "6"
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert "--system-prompt" not in cmd


def test_extra_turns_are_ignored_without_web():
    """A tool-less call cannot use extra turns for anything useful — do not let a caller pay for them."""
    cmd = cmdline.build_cmd("claude", "", "opus", max_turns=9)
    assert cmd[cmd.index("--max-turns") + 1] == "1"
