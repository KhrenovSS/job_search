from pathlib import Path

import pytest

from hh_scout.browser.hh_pages import extract_initial_state

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def search_state():
    return extract_initial_state((FIXTURES / "search_page.html").read_text(encoding="utf-8", errors="replace"))


@pytest.fixture(scope="session")
def vacancy_state():
    return extract_initial_state((FIXTURES / "vacancy_page.html").read_text(encoding="utf-8", errors="replace"))
