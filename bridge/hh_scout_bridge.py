"""HH-Scout Claude bridge.

A tiny FastAPI service (systemd `hh-scout-bridge`) that turns an HTTP request into a single
headless `claude -p` call under the owner's subscription. hh-scout calls it at http://127.0.0.1:<port>.

Endpoints:
    GET  /health    -> {"ok": true, "model": "<default model>"}
    POST /complete  -> {"text": str, "usage": {...}, "cost_usd": float | null}

Auth: header `X-Bridge-Token` must equal HH_BRIDGE_TOKEN from the environment.

Environment (see .env.bridge.example):
    HH_BRIDGE_TOKEN   shared secret, required (an empty token rejects every request)
    BRIDGE_MODEL      default model alias/id passed to `claude --model` (default: opus)
    BRIDGE_TIMEOUT    seconds to wait for the CLI before answering 504 (default: 150)
    BRIDGE_WORKDIR    cwd for the CLI; must NOT contain a CLAUDE.md (default: system temp dir)
    CLAUDE_BIN        path to the claude executable (default: "claude" from PATH)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("hh_scout_bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BRIDGE_TOKEN = os.environ.get("HH_BRIDGE_TOKEN", "")
BRIDGE_MODEL = os.environ.get("BRIDGE_MODEL", "opus")
BRIDGE_TIMEOUT = float(os.environ.get("BRIDGE_TIMEOUT", "150"))
BRIDGE_WORKDIR = os.environ.get("BRIDGE_WORKDIR") or tempfile.gettempdir()
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

MAX_PROMPT_CHARS = 200_000
MAX_MESSAGES = 20

app = FastAPI(title="hh-scout bridge", docs_url=None, redoc_url=None)


class Message(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=MAX_PROMPT_CHARS)


class CompleteRequest(BaseModel):
    system_text: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    messages: list[Message] = Field(min_length=1, max_length=MAX_MESSAGES)
    model: str = ""  # empty -> BRIDGE_MODEL
    max_tokens: int = 8000  # accepted for API compatibility; the CLI decides


class CompleteResponse(BaseModel):
    text: str
    usage: dict[str, Any]
    cost_usd: float | None = None


def _check_token(token: str | None) -> None:
    if not BRIDGE_TOKEN or token != BRIDGE_TOKEN:
        raise HTTPException(status_code=401, detail="bad bridge token")


def _flatten(messages: list[Message]) -> str:
    """Collapse a chat history into one prompt for the single-turn CLI call."""
    if len(messages) == 1:
        return messages[0].content
    labels = {"user": "Пользователь", "assistant": "Ассистент (твой прошлый ответ)"}
    return "\n\n".join(f"{labels[m.role]}:\n{m.content}" for m in messages)


def _cli_env() -> dict[str, str]:
    env = dict(os.environ)
    # Never let the CLI believe it is nested inside an interactive Claude Code session.
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    return env


async def run_claude(system_text: str, prompt: str, model: str) -> dict[str, Any]:
    cmd = [CLAUDE_BIN, "-p", "--output-format", "json", "--max-turns", "1", "--tools", "", "--model", model]
    if system_text:
        cmd += ["--system-prompt", system_text]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=BRIDGE_WORKDIR,
        env=_cli_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout=BRIDGE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("claude CLI timed out after %.0fs (model=%s)", BRIDGE_TIMEOUT, model)
        raise HTTPException(status_code=504, detail="claude CLI timeout")

    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace")[-800:]
        log.error("claude CLI exit %s: %s", proc.returncode, tail)
        raise HTTPException(status_code=502, detail=f"claude CLI exit {proc.returncode}: {tail}")
    try:
        envelope = json.loads(out.decode("utf-8"))
    except json.JSONDecodeError:
        log.error("claude CLI returned non-JSON: %r", out[:300])
        raise HTTPException(status_code=502, detail="claude CLI returned non-JSON envelope")
    if envelope.get("is_error"):
        log.error("claude CLI reported error: %s", str(envelope.get("result"))[:500])
        raise HTTPException(status_code=502, detail=f"claude error: {str(envelope.get('result'))[:500]}")
    return envelope


def _envelope_to_response(envelope: dict[str, Any]) -> CompleteResponse:
    usage = envelope.get("usage") or {}
    return CompleteResponse(
        text=str(envelope.get("result", "")),
        usage={
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        },
        cost_usd=envelope.get("total_cost_usd"),
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "model": BRIDGE_MODEL}


@app.post("/complete", response_model=CompleteResponse)
async def complete(req: CompleteRequest, x_bridge_token: str | None = Header(default=None)) -> CompleteResponse:
    _check_token(x_bridge_token)
    model = req.model or BRIDGE_MODEL
    prompt = _flatten(req.messages)
    log.info("complete: model=%s system=%d chars prompt=%d chars", model, len(req.system_text), len(prompt))
    envelope = await run_claude(req.system_text, prompt, model)
    resp = _envelope_to_response(envelope)
    log.info("complete: done, %d chars out, cost=%s", len(resp.text), resp.cost_usd)
    return resp
