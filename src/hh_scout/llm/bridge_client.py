"""HTTP client for the project's Claude bridge (bridge/hh_scout_bridge.py)."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from hh_scout.config import Settings

log = logging.getLogger(__name__)

_TRANSIENT = {502, 503, 504}
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class BridgeError(RuntimeError):
    """Non-retryable bridge failure (bad token, bad request, exhausted retries)."""


class BridgeUnavailable(BridgeError):
    """The bridge could not be reached or kept failing transiently."""


def extract_json(text: str) -> Any:
    """Return the first JSON value found in a model answer (fenced or bare)."""
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text]
    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
        # bare text with a JSON array/object somewhere inside
        for opener, closer in (("[", "]"), ("{", "}")):
            start, end = cand.find(opener), cand.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(cand[start:end + 1])
                except json.JSONDecodeError:
                    continue
    raise ValueError("в ответе модели нет корректного JSON")


class BridgeClient:
    def __init__(self, settings: Settings, *, retries: int = 2, backoff_s: float = 2.0, sleep=time.sleep) -> None:
        self._url = settings.bridge_url.rstrip("/")
        self._token = settings.bridge_token
        self._model = settings.bridge_model
        self._timeout = settings.bridge_timeout_s
        self._retries = retries
        self._backoff = backoff_s
        self._sleep = sleep
        self.calls = 0
        self.cost_usd = 0.0

    def health(self) -> dict[str, Any]:
        r = httpx.get(f"{self._url}/health", timeout=10)
        r.raise_for_status()
        return r.json()

    def complete(self, system_text: str, user_text: str, *, model: str | None = None) -> str:
        payload = {
            "system_text": system_text,
            "messages": [{"role": "user", "content": user_text}],
            "model": model or self._model or "",
        }
        last_err: str = ""
        for attempt in range(self._retries + 1):
            try:
                resp = httpx.post(f"{self._url}/complete", json=payload,
                                  headers={"X-Bridge-Token": self._token}, timeout=self._timeout)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = f"{type(e).__name__}: {e}"
            else:
                self.calls += 1
                if resp.status_code == 200:
                    data = resp.json()
                    self.cost_usd += float(data.get("cost_usd") or 0.0)
                    return str(data.get("text", ""))
                if resp.status_code not in _TRANSIENT:
                    raise BridgeError(f"мост ответил {resp.status_code}: {resp.text[:300]}")
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if attempt < self._retries:
                delay = self._backoff * (attempt + 1)
                log.warning("Мост: временная ошибка (%s), повтор через %.0f с", last_err, delay)
                self._sleep(delay)
        raise BridgeUnavailable(f"мост недоступен после {self._retries + 1} попыток: {last_err}")
