"""Attach to the owner's already-running Firefox through geckodriver --connect-existing.

Firefox must have been started with `--marionette` (see scripts/setup_firefox.sh); Marionette
listens on 127.0.0.1:2828. For every pipeline run we spawn a short-lived geckodriver that
connects to that port, open our own browser window, browse, close the window and detach.
The owner's tabs are never touched: we never switch into them and never run scripts there.

The window we open is remembered by its handle in the `kv` table (`WindowRegistry`), not by anything the page
can see: `window.name` used to carry a literal "hh-scout-bot" label that any script on hh.ru could read (v9.11).
"""

from __future__ import annotations

import logging
import random
import shutil
import socket
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from collections.abc import Sequence
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException

from hh_scout.browser import pacing
from hh_scout.browser.hh_pages import HH_STATE_MARKER, extract_initial_state
from hh_scout.config import Settings
from hh_scout.db import kv_get, kv_set

log = logging.getLogger(__name__)

# Navigation waits in two steps: a short page load (the markup we need is server-rendered and
# arrives with the document), then a poll for the markup itself. See decision #39.
PAGE_LOAD_TIMEOUT_S = 15.0
MARKUP_WAIT_S = 20.0
HH_HOST = "https://hh.ru"
WINDOW_HANDLE_KEY = "bot_window_handle"


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


class WindowRegistry:
    """Where the handle of our own window is kept between processes, so an interrupted run can be tidied up.

    Kept in `kv` (one row), invisible to the page. Only a pipeline run may reap a stray window (`reap=True`):
    it holds the `runs.status='running'` guard, so a remembered handle can only be a leftover. Diagnostic CLIs
    (`check_browser.py`) never reap — the handle they would find may belong to a live sitting.
    """

    def __init__(self, conn: sqlite3.Connection, *, reap: bool = False) -> None:
        self.conn = conn
        self.reap = reap

    def remembered(self) -> str | None:
        return kv_get(self.conn, WINDOW_HANDLE_KEY)

    def remember(self, handle: str | None) -> None:
        with self.conn:
            kv_set(self.conn, WINDOW_HANDLE_KEY, handle)


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


class BrowserSession:
    """Context manager around geckodriver + Selenium Remote for an existing Firefox.

    `page_budget` is how many pages this session may load; there is no implicit default — a caller that
    forgets it gets 0, not the whole day's cap (v9.11).
    """

    def __init__(self, settings: Settings, *, page_budget: int = 0, rng: random.Random | None = None,
                 registry: WindowRegistry | None = None) -> None:
        self._s = settings
        self._proc: subprocess.Popen[bytes] | None = None
        self._driver: webdriver.Remote | None = None
        self._own_window: str | None = None
        self.page_loads = 0
        self.page_budget = page_budget
        self._rng = rng or random.Random()
        self.policy = pacing.policy_from_settings(settings)
        self._registry = registry

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
        # 'eager' returns from get() at DOMContentLoaded instead of waiting for every analytics
        # request hh.ru keeps open: the pages we read ship their JSON in the server-rendered HTML,
        # so the data is already there. With the default 'normal' every load burned the full
        # page-load timeout (see decision #39).
        opts.page_load_strategy = "eager"
        try:
            self._driver = webdriver.Remote(command_executor=f"http://127.0.0.1:{gd_port}", options=opts)
        except WebDriverException as e:
            self._kill_driver()
            raise BrowserUnavailable(f"не удалось открыть сессию Marionette: {e.msg}") from e
        log.info("Подключились к Firefox %s через geckodriver :%d", self.info().browser_version, gd_port)
        self.reap_stray_window()
        return self

    def reap_stray_window(self) -> bool:
        """Close the window an interrupted run left behind, if the registry allows it and it still exists.

        Only the remembered handle is touched — never any other window, so the owner's tabs stay where they are.
        """
        if self._registry is None or not self._registry.reap:
            return False
        handle = self._registry.remembered()
        if not handle:
            return False
        d = self.driver
        closed = False
        try:
            if handle in d.window_handles and len(d.window_handles) > 1:
                d.switch_to.window(handle)
                d.close()
                d.switch_to.window(d.window_handles[0])
                log.info("Закрыто окно, оставшееся от прерванного прогона")
                closed = True
        except WebDriverException as e:
            log.warning("Не удалось закрыть старое окно бота: %s", e.msg)
        self._registry.remember(None)
        return closed

    def close(self) -> None:
        """Close our window (if any) and detach. The owner's Firefox keeps running."""
        if self._driver is not None:
            try:
                if self._own_window and self._own_window in self._driver.window_handles:
                    self._driver.switch_to.window(self._own_window)
                    if len(self._driver.window_handles) > 1:
                        self._driver.close()
                        self._own_window = None
            except WebDriverException as e:
                log.warning("Не удалось закрыть своё окно: %s", e.msg)
            # Do NOT call driver.quit(): in --connect-existing mode geckodriver would ask
            # Firefox to shut down. Killing geckodriver simply detaches the session.
            self._driver = None
        if self._registry is not None and self._own_window is None:
            self._registry.remember(None)   # a window we could not close stays remembered for the next run
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
        if self._registry is not None:
            self._registry.remember(self._own_window)

    # -- browsing ------------------------------------------------------------

    def _wait_for_markers(self, markers: Sequence[str]) -> bool:
        """Poll the live DOM until one of `markers` shows up, at most MARKUP_WAIT_S seconds.

        Normally returns on the first check: the markup is server-rendered and already in place
        when navigation returns. This is what makes the short page-load timeout safe.
        """
        d = self.driver
        script = ("const h = document.documentElement.innerHTML;"
                  "return arguments[0].some(m => h.indexOf(m) !== -1);")
        wanted = list(markers)
        deadline = time.monotonic() + MARKUP_WAIT_S
        while True:
            try:
                # the check runs in the page: shipping the whole innerHTML back on every poll would cost
                # more than the wait it is saving
                found = bool(d.execute_script(script, wanted))
            except WebDriverException:
                found = False
            if found:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)

    def _navigate(self, url: str) -> None:
        """Go to `url` the way a person's browser would.

        From a page on the same site the move is made by the page itself (`location.assign`), so the request
        carries a Referer like a click would; `driver.get()` sends none, and twenty referrer-less loads of
        `search/vacancy?…&page=N` in a row read as a script (v9.11). The first page of a window, a move to
        another host, or a navigation that does not commit within PAGE_LOAD_TIMEOUT_S fall back to `get()`.
        """
        d = self.driver
        d.set_page_load_timeout(PAGE_LOAD_TIMEOUT_S)
        try:
            current = str(d.current_url or "")
        except WebDriverException:
            current = ""
        same_site = current.startswith(HH_HOST) and url.startswith(HH_HOST) and current != url
        if same_site:
            try:
                d.execute_script("location.assign(arguments[0]);", url)
                deadline = time.monotonic() + PAGE_LOAD_TIMEOUT_S
                while time.monotonic() < deadline:
                    time.sleep(0.25)
                    try:
                        if d.current_url != current and d.execute_script("return document.readyState") != "loading":
                            return
                    except WebDriverException:
                        pass
                log.debug("Переход из страницы не завершился за %.0f с — открываю адресом: %s", PAGE_LOAD_TIMEOUT_S, url)
            except WebDriverException as e:
                log.debug("location.assign не сработал (%s) — открываю адресом", e.msg)
        try:
            d.get(url)
        except TimeoutException:
            log.debug("get() не вернулся за %.0f с — ждём разметку: %s", PAGE_LOAD_TIMEOUT_S, url)

    def open_raw(self, url: str, wait_for: Sequence[str] = ()) -> str:
        """Load any page in our own window like a person would and return its rendered HTML.

        Counts against the page budget, waits until the page actually carries what the caller needs,
        lets the SPA settle, scrolls a bit, then waits a human-like "reading" pause.
        `wait_for` is a set of substrings; the first one to appear ends the wait.
        Raises PageBudgetExceeded; site-specific checks are up to the caller (see `open()` for hh.ru).
        """
        if self.page_loads >= self.page_budget:
            raise PageBudgetExceeded(f"лимит {self.page_budget} загрузок страниц за прогон исчерпан")
        self.open_own_window()
        d = self.driver
        # Counted before the request leaves: a load that fails half-way still happened on hh's side.
        self.page_loads += 1
        self._navigate(url)
        if wait_for and not self._wait_for_markers(wait_for):
            log.warning("Страница без ожидаемой разметки за %.0f с: %s", MARKUP_WAIT_S, url)
        pacing.sleep(self._rng.uniform(1.5, 3.5))  # let the SPA settle
        pacing.scroll_like_human(d, self._rng)
        source = d.page_source
        delay = pacing.page_delay(self.policy, self._rng)
        log.debug("Загрузка %d/%d: %s — пауза %.0f с", self.page_loads, self.page_budget, url, delay)
        pacing.sleep(delay)
        return source

    def open(self, url: str) -> dict[str, Any]:
        """Load a hh.ru page and return its HH-Lux initial state (see `open_raw` for the browsing part).

        Raises PageBudgetExceeded / HHBlocked; the caller decides how to stop softly.
        """
        source = self.open_raw(url, wait_for=(HH_STATE_MARKER,))
        state = extract_initial_state(source)
        if state is None:
            d = self.driver
            title = d.title
            cur = d.current_url
            log.warning("Нет HH-Lux-InitialState: title=%r url=%s", title, cur)
            raise HHBlocked(f"hh.ru вернул страницу без данных (заголовок: {title!r}, адрес: {cur}) — возможно капча или требуется вход")
        return state
