"""Shared fixtures for anime-pahe-dl test suite."""

from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from anime_pahe_dl.client import Source


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    """Redirect all file I/O (config, cookies, history) to a temp directory."""
    monkeypatch.setattr("anime_pahe_dl.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("anime_pahe_dl.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("anime_pahe_dl.client.COOKIE_CACHE_DIR", tmp_path)
    monkeypatch.setattr("anime_pahe_dl.client.COOKIE_CACHE_FILE", tmp_path / "cookies.json")
    monkeypatch.setattr("anime_pahe_dl.cli.HISTORY_DIR", tmp_path)
    monkeypatch.setattr("anime_pahe_dl.cli.HISTORY_FILE", tmp_path / "history.json")
    return tmp_path


@pytest.fixture
def sample_anime_api_response():
    """Sample AnimePahe search API response."""
    return {
        "data": [
            {
                "id": 12345,
                "session": "abc123sess",
                "title": "Zom 100: Bucket List of the Dead",
                "episodes": 12,
                "status": "Currently Airing",
                "year": 2023,
                "poster": "https://example.com/poster.jpg",
            },
            {
                "id": 67890,
                "session": "def456sess",
                "title": "Bleach: Thousand-Year Blood War",
                "episodes": 26,
                "status": "Completed",
                "year": 2022,
                "poster": "https://example.com/poster2.jpg",
            },
        ]
    }


@pytest.fixture
def sample_episode_api_response():
    """Sample AnimePahe episodes API response (single page)."""
    return {
        "total": 3,
        "per_page": 30,
        "current_page": 1,
        "last_page": 1,
        "data": [
            {"episode": 1, "session": "ep_sess_1", "title": "", "snapshot": "", "filler": 0},
            {"episode": 2, "session": "ep_sess_2", "title": "", "snapshot": "", "filler": 0},
            {"episode": 3, "session": "ep_sess_3", "title": "", "snapshot": "", "filler": 1},
        ],
    }


@pytest.fixture
def sample_sources():
    """Sample download sources with varied qualities and audio."""
    return [
        Source(url="https://pahe.win/aaa", quality="360p", audio="jpn", fansub="SubsPlease", size="50MB"),
        Source(url="https://pahe.win/bbb", quality="720p", audio="jpn", fansub="SubsPlease", size="120MB"),
        Source(url="https://pahe.win/ccc", quality="1080p", audio="jpn", fansub="Judas", size="250MB"),
        Source(url="https://pahe.win/ddd", quality="720p", audio="eng", fansub="Funimation", size="130MB"),
    ]


@pytest.fixture
def mock_playwright(mocker):
    """Build a complete Playwright mock chain.

    Returns a dict of all mocks for easy assertion:
    {pw, browser, context, page, sync_playwright_cls}
    """
    mock_page = MagicMock()
    mock_page.goto.return_value = None
    mock_page.title.return_value = "AnimePahe"
    mock_page.content.return_value = "<html></html>"
    mock_page.evaluate.return_value = "Mozilla/5.0 Test"
    mock_page.eval_on_selector.return_value = None
    mock_page.eval_on_selector_all.return_value = []
    mock_page.query_selector.return_value = None
    mock_page.wait_for_selector.return_value = None
    mock_page.wait_for_function.return_value = None
    mock_page.wait_for_timeout.return_value = None
    mock_page.close.return_value = None

    mock_context = MagicMock()
    mock_context.new_page.return_value = mock_page
    mock_context.cookies.return_value = [
        {"name": "cf_clearance", "value": "abc123", "domain": ".animepahe.si", "path": "/"},
    ]

    mock_browser = MagicMock()
    mock_browser.new_context.return_value = mock_context

    mock_pw = MagicMock()
    mock_pw.chromium.launch.return_value = mock_browser

    mock_sync_pw = MagicMock()
    mock_sync_pw.start.return_value = mock_pw

    # Patch in both modules (they import locally)
    mocker.patch("anime_pahe_dl.client.sync_playwright", return_value=mock_sync_pw, create=True)
    mocker.patch("anime_pahe_dl.downloader.sync_playwright", return_value=mock_sync_pw, create=True)

    return {
        "pw": mock_pw,
        "browser": mock_browser,
        "context": mock_context,
        "page": mock_page,
        "sync_playwright_cls": mock_sync_pw,
    }


@pytest.fixture
def cli_runner():
    """Click test runner."""
    return CliRunner(mix_stderr=False)
