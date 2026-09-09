"""Attach to the owner's already-running Firefox through geckodriver --connect-existing.

Firefox must have been started with `--marionette` (see scripts/setup_firefox.sh); Marionette
listens on 127.0.0.1:2828. For every pipeline run we spawn a short-lived geckodriver that
connects to that port, open our own browser window, browse, close the window and detach.
The owner's tabs are never touched.
"""

from __future__ import annotations

import logging
import random
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException

from hh_scout.browser import pacing
from hh_scout.browser.hh_pages import extract_initial_state
from hh_scout.config import Settings

log = logging.getLogger(__name__)


class BrowserUnavailable(RuntimeError):
    """Firefox is not running with --marionette, or geckodriver is missing."""


class PageBudgetExceeded(RuntimeError):
    """The per-run page-load limit was reached; stop browsing for today."""


class HHBlocked(RuntimeError):
    """hh.ru answered with a captcha / login page / something without the initial state."""


@dataclass
class BrowserInfo:
    browser_version: str
    profile: str
    windows: int
    current_url: str
    title: str


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


BOT_WINDOW_NAME = "hh-scout-bot"


class BrowserSession:
    """Context manager around geckodriver + Selenium Remote for an existing Firefox."""

    def __init__(self, settings: Settings, *, page_budget: int | None = None, rng: random.Random | None = None) -> None:
        self._s = settings
        self._proc: subprocess.Popen[bytes] | None = None
        self._driver: webdriver.Remote | None = None
        self._own_window: str | None = None
        self.page_loads = 0
        self.page_budget = page_budget if page_budget is not None else settings.daily_page_loads_max
        self._rng = rng or random.Random()
        self.policy = pacing.policy_from_settings(settings)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "BrowserSession":
        host, port = self._s.marionette_host, self._s.marionette_port
        if not _port_open(host, port):
            raise BrowserUnavailable(
                f"Marionette не отвечает на {host}:{port} — Firefox не запущен с флагом --marionette"
            )
        gd = self._s.geckodriver_path or shutil.which("geckodriver")
        if not gd:
            raise BrowserUnavailable("geckodriver не найден — запустите scripts/install_geckodriver.sh")

        gd_port = _free_port()
        cmd = [
            gd, "--connect-existing",
            "--marionette-host", host, "--marionette-port", str(port),
            "--host", "127.0.0.1", "--port", str(gd_port),
            "--log", "warn",
        ]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while not _port_open("127.0.0.1", gd_port, 0.2):
            if self._proc.poll() is not None or time.monotonic() > deadline:
                err = (self._proc.stderr.read().decode("utf-8", "replace") if self._proc.stderr else "")[-500:]
                self._kill_driver()
                raise BrowserUnavailable(f"geckodriver не стартовал: {err or 'timeout'}")
            time.sleep(0.1)

        opts = webdriver.FirefoxOptions()
        try:
            self._driver = webdriver.Remote(command_executor=f"http://127.0.0.1:{gd_port}", options=opts)
        except WebDriverException as e:
            self._kill_driver()
            raise BrowserUnavailable(f"не удалось открыть сессию Marionette: {e.msg}") from e
        log.info("Подключились к Firefox %s через geckodriver :%d", self.info().browser_version, gd_port)
        self.close_stray_bot_windows()
        return self

    def close_stray_bot_windows(self) -> int:
        """Close windows left behind by an interrupted run (marked with window.name)."""
        d = self.driver
        closed = 0
        try:
            handles = list(d.window_handles)
            for h in handles:
                if len(d.window_handles) <= 1:
                    break
                d.switch_to.window(h)
                try:
                    name = d.execute_script("return window.name")
                except WebDriverException:
                    continue
                if name == BOT_WINDOW_NAME:
                    d.close()
                    closed += 1
            if d.window_handles:
                d.switch_to.window(d.window_handles[0])
        except WebDriverException as e:
            log.warning("Не удалось проверить старые окна бота: %s", e.msg)
        if closed:
            log.info("Закрыто окон, оставшихся от прерванного прогона: %d", closed)
        return closed

    def close(self) -> None:
        """Close our window (if any) and detach. The owner's Firefox keeps running."""
        if self._driver is not None:
            try:
                if self._own_window and self._own_window in self._driver.window_handles:
                    self._driver.switch_to.window(self._own_window)
                    if len(self._driver.window_handles) > 1:
                        self._driver.close()
            except WebDriverException as e:
                log.warning("Не удалось закрыть своё окно: %s", e.msg)
            # Do NOT call driver.quit(): in --connect-existing mode geckodriver would ask
            # Firefox to shut down. Killing geckodriver simply detaches the session.
            self._driver = None
        self._kill_driver()

    def _kill_driver(self) -> None:
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- queries -------------------------------------------------------------

    @property
    def driver(self) -> webdriver.Remote:
        if self._driver is None:
            raise BrowserUnavailable("сессия браузера не открыта")
        return self._driver

    def info(self) -> BrowserInfo:
        d = self.driver
        caps = d.capabilities
        return BrowserInfo(
            browser_version=str(caps.get("browserVersion", "?")),
            profile=str(caps.get("moz:profile", "?")),
            windows=len(d.window_handles),
            current_url=d.current_url,
            title=d.title,
        )

    def open_own_window(self) -> None:
        """Open a dedicated window so we never navigate the owner's tabs."""
        d = self.driver
        if self._own_window and self._own_window in d.window_handles:
            d.switch_to.window(self._own_window)
            return
        before = set(d.window_handles)
        d.switch_to.new_window("window")
        new = set(d.window_handles) - before
        self._own_window = next(iter(new)) if new else d.current_window_handle
        d.switch_to.window(self._own_window)
        try:
            d.execute_script("window.name = arguments[0];", BOT_WINDOW_NAME)
        except WebDriverException as e:
            log.debug("Не удалось пометить окно: %s", e.msg)

    # -- browsing ------------------------------------------------------------

    def open(self, url: str) -> dict[str, Any]:
        """Load a hh.ru page in our own window like a person would and return its initial state.

        Counts against the page budget, waits a human-like pause, scrolls a bit.
        Raises PageBudgetExceeded / HHBlocked; the caller decides how to stop softly.
        """
        if self.page_loads >= self.page_budget:
            raise PageBudgetExceeded(f"лимит {self.page_budget} загрузок страниц за прогон исчерпан")
        self.open_own_window()
        d = self.driver
        d.set_page_load_timeout(60)
        try:
            d.get(url)
        except TimeoutException:
            log.warning("Страница не загрузилась за 60 с: %s", url)
        self.page_loads += 1
        try:
            d.execute_script("window.name = arguments[0];", BOT_WINDOW_NAME)
        except WebDriverException:
            pass
        pacing.sleep(self._rng.uniform(1.5, 3.5))  # let the SPA settle
        pacing.scroll_like_human(d, self._rng)
        source = d.page_source
        state = extract_initial_state(source)
        if state is None:
            title = d.title
            cur = d.current_url
            log.warning("Нет HH-Lux-InitialState: title=%r url=%s", title, cur)
            raise HHBlocked(f"hh.ru вернул страницу без данных (заголовок: {title!r}, адрес: {cur}) — возможно капча или требуется вход")
        delay = pacing.page_delay(self.policy, self._rng)
        log.debug("Загрузка %d/%d: %s — пауза %.0f с", self.page_loads, self.page_budget, url, delay)
        pacing.sleep(delay)
        return state
