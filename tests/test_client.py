"""Tests for anime_pahe_dl.client module."""

import json
import time
from unittest.mock import MagicMock

import requests

from anime_pahe_dl.client import (
    COOKIE_MAX_AGE,
    MIRRORS,
    Anime,
    AnimePaheClient,
    Episode,
    Source,
)


# ── Dataclass tests ──────────────────────────────────────────────


class TestAnimeFromApi:
    def test_full_data(self):
        data = {
            "id": 123,
            "session": "sess1",
            "title": "Naruto",
            "episodes": 220,
            "status": "Completed",
            "year": 2002,
            "poster": "https://example.com/p.jpg",
        }
        anime = Anime.from_api(data)
        assert anime.id == 123
        assert anime.session == "sess1"
        assert anime.title == "Naruto"
        assert anime.episodes == 220
        assert anime.status == "Completed"
        assert anime.year == "2002"
        assert anime.poster == "https://example.com/p.jpg"

    def test_missing_fields(self):
        anime = Anime.from_api({})
        assert anime.id == 0
        assert anime.session == ""
        assert anime.title == "Unknown"
        assert anime.episodes == 0
        assert anime.status == "Unknown"

    def test_partial_data(self):
        anime = Anime.from_api({"title": "Bleach", "episodes": 366})
        assert anime.title == "Bleach"
        assert anime.episodes == 366
        assert anime.id == 0  # default


class TestSourceIsDub:
    def test_eng(self):
        assert Source(url="", quality="720p", audio="eng").is_dub is True

    def test_english(self):
        assert Source(url="", quality="720p", audio="English").is_dub is True

    def test_jpn_not_dub(self):
        assert Source(url="", quality="720p", audio="jpn").is_dub is False

    def test_japanese_not_dub(self):
        assert Source(url="", quality="720p", audio="Japanese").is_dub is False


class TestEpisodeDataclass:
    def test_defaults(self):
        ep = Episode(number=1, session="x")
        assert ep.title == ""
        assert ep.snapshot == ""
        assert ep.filler is False

    def test_filler_flag(self):
        ep = Episode(number=5, session="y", filler=True)
        assert ep.filler is True


# ── Cookie cache tests ───────────────────────────────────────────


class TestLoadCachedCookies:
    def test_no_file(self, tmp_config_dir):
        client = AnimePaheClient()
        # Should not crash — cookies file doesn't exist
        assert client._base_url is None

    def test_valid_cookies(self, tmp_config_dir, monkeypatch):
        now = time.time()
        cache = {
            "saved_at": now - 60,  # 1 minute ago
            "base_url": "https://animepahe.si",
            "cookies": [
                {"name": "cf_clearance", "value": "tok123", "domain": ".animepahe.si", "path": "/"},
            ],
        }
        (tmp_config_dir / "cookies.json").write_text(json.dumps(cache))

        client = AnimePaheClient()
        assert client._base_url == "https://animepahe.si"
        # Cookie should be loaded into session
        assert client._http.cookies.get("cf_clearance") == "tok123"

    def test_expired_cookies(self, tmp_config_dir):
        cache = {
            "saved_at": time.time() - (COOKIE_MAX_AGE + 60),  # expired
            "base_url": "https://animepahe.si",
            "cookies": [
                {"name": "cf_clearance", "value": "old_tok", "domain": ".animepahe.si", "path": "/"},
            ],
        }
        (tmp_config_dir / "cookies.json").write_text(json.dumps(cache))

        client = AnimePaheClient()
        # Expired — cookies should NOT be loaded
        assert client._http.cookies.get("cf_clearance") is None

    def test_corrupt_json(self, tmp_config_dir):
        (tmp_config_dir / "cookies.json").write_text("{bad json!!")
        # Should not crash
        client = AnimePaheClient()
        assert client._base_url is None

    def test_empty_cookies_list(self, tmp_config_dir):
        cache = {"saved_at": time.time(), "cookies": [], "base_url": "https://animepahe.si"}
        (tmp_config_dir / "cookies.json").write_text(json.dumps(cache))

        client = AnimePaheClient()
        # No cookies to load, but base_url should not be set either
        # (the code returns early before setting base_url)
        assert client._base_url is None


class TestSaveCookiesToCache:
    def test_no_context(self, tmp_config_dir):
        client = AnimePaheClient()
        client._pw_context = None
        client._save_cookies_to_cache()
        assert not (tmp_config_dir / "cookies.json").exists()

    def test_writes_file(self, tmp_config_dir):
        client = AnimePaheClient()
        client._base_url = "https://animepahe.si"

        mock_ctx = MagicMock()
        mock_ctx.cookies.return_value = [
            {"name": "cf_clearance", "value": "saved_tok", "domain": ".animepahe.si", "path": "/"},
        ]
        client._pw_context = mock_ctx

        client._save_cookies_to_cache()

        cache_file = tmp_config_dir / "cookies.json"
        assert cache_file.exists()
        data = json.loads(cache_file.read_text())
        assert "saved_at" in data
        assert data["base_url"] == "https://animepahe.si"
        assert len(data["cookies"]) == 1
        assert data["cookies"][0]["name"] == "cf_clearance"


# ── HTTP helper tests ────────────────────────────────────────────


class TestApiGet:
    def test_success_first_mirror(self, tmp_config_dir):
        client = AnimePaheClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"data": []}'
        mock_resp.json.return_value = {"data": []}
        client._http.get = MagicMock(return_value=mock_resp)

        result = client._api_get({"m": "search", "q": "test"})
        assert result == {"data": []}
        assert client._base_url == MIRRORS[0]

    def test_tries_next_mirror_on_failure(self, tmp_config_dir):
        client = AnimePaheClient()

        fail_resp = MagicMock()
        fail_resp.status_code = 200
        fail_resp.content = b"<html>CF challenge</html>"
        fail_resp.json.side_effect = ValueError("Not JSON")

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.content = b'{"data": []}'
        ok_resp.json.return_value = {"data": []}

        client._http.get = MagicMock(side_effect=[fail_resp, ok_resp])

        result = client._api_get({"m": "search"})
        assert result == {"data": []}
        assert client._base_url == MIRRORS[1]

    def test_all_mirrors_fail(self, tmp_config_dir):
        client = AnimePaheClient()
        client._http.get = MagicMock(side_effect=requests.ConnectionError("down"))

        result = client._api_get({"m": "search"})
        assert result is None

    def test_404_response(self, tmp_config_dir):
        client = AnimePaheClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        client._http.get = MagicMock(return_value=mock_resp)

        result = client._api_get({"m": "search"})
        assert result is None


class TestApiGetWithFallback:
    def test_http_success_skips_playwright(self, tmp_config_dir):
        client = AnimePaheClient()
        client._api_get = MagicMock(return_value={"data": []})
        client._ensure_playwright = MagicMock()

        result = client._api_get_with_fallback({"m": "search"})
        assert result == {"data": []}
        client._ensure_playwright.assert_not_called()

    def test_falls_back_to_playwright(self, tmp_config_dir):
        client = AnimePaheClient()
        client._api_get = MagicMock(return_value=None)
        client._ensure_playwright = MagicMock()
        client._playwright_json = MagicMock(return_value={"data": [{"id": 1}]})
        client._transfer_cookies_to_http = MagicMock()
        client._save_cookies_to_cache = MagicMock()

        result = client._api_get_with_fallback({"m": "search"})
        assert result == {"data": [{"id": 1}]}
        client._ensure_playwright.assert_called_once()
        client._transfer_cookies_to_http.assert_called_once()
        client._save_cookies_to_cache.assert_called_once()


class TestTransferCookies:
    def test_no_context(self, tmp_config_dir):
        client = AnimePaheClient()
        client._pw_context = None
        # Should not crash
        client._transfer_cookies_to_http()

    def test_copies_to_session(self, tmp_config_dir):
        client = AnimePaheClient()
        mock_ctx = MagicMock()
        mock_ctx.cookies.return_value = [
            {"name": "cf_clearance", "value": "val1", "domain": ".animepahe.si", "path": "/"},
            {"name": "__cf_bm", "value": "val2", "domain": ".animepahe.si", "path": "/"},
        ]
        client._pw_context = mock_ctx

        client._transfer_cookies_to_http()
        assert client._http.cookies.get("cf_clearance") == "val1"
        assert client._http.cookies.get("__cf_bm") == "val2"


# ── Public API tests ─────────────────────────────────────────────


class TestSearch:
    def test_returns_anime_list(self, tmp_config_dir, sample_anime_api_response):
        client = AnimePaheClient()
        client._api_get_with_fallback = MagicMock(return_value=sample_anime_api_response)

        results = client.search("zom")
        assert len(results) == 2
        assert isinstance(results[0], Anime)
        assert results[0].title == "Zom 100: Bucket List of the Dead"

    def test_no_results(self, tmp_config_dir):
        client = AnimePaheClient()
        client._api_get_with_fallback = MagicMock(return_value=None)

        assert client.search("nonexistent") == []

    def test_no_data_key(self, tmp_config_dir):
        client = AnimePaheClient()
        client._api_get_with_fallback = MagicMock(return_value={"total": 0})

        assert client.search("test") == []


class TestGetEpisodes:
    def test_single_page(self, tmp_config_dir, sample_episode_api_response):
        client = AnimePaheClient()
        client._api_get_with_fallback = MagicMock(return_value=sample_episode_api_response)

        eps = client.get_episodes("sess1")
        assert len(eps) == 3
        assert eps[0].number == 1
        assert eps[0].session == "ep_sess_1"
        assert eps[2].filler is True

    def test_multi_page(self, tmp_config_dir):
        page1 = {
            "total": 4,
            "per_page": 2,
            "last_page": 2,
            "data": [
                {"episode": 1, "session": "s1", "filler": 0},
                {"episode": 2, "session": "s2", "filler": 0},
            ],
        }
        page2 = {
            "total": 4,
            "per_page": 2,
            "last_page": 2,
            "data": [
                {"episode": 3, "session": "s3", "filler": 0},
                {"episode": 4, "session": "s4", "filler": 0},
            ],
        }
        client = AnimePaheClient()
        client._api_get_with_fallback = MagicMock(side_effect=[page1, page2])

        eps = client.get_episodes("sess1")
        assert len(eps) == 4
        assert eps[3].session == "s4"

    def test_empty(self, tmp_config_dir):
        client = AnimePaheClient()
        client._api_get_with_fallback = MagicMock(return_value=None)

        assert client.get_episodes("sess1") == []


class TestGetEpisodeSession:
    def test_first_page(self, tmp_config_dir, sample_episode_api_response):
        client = AnimePaheClient()
        client.get_episode_page = MagicMock(return_value=sample_episode_api_response)

        result = client.get_episode_session("sess1", 2)
        assert result == "ep_sess_2"

    def test_later_page(self, tmp_config_dir):
        page1 = {"total": 35, "per_page": 30, "last_page": 2, "data": [{"session": f"s{i}"} for i in range(30)]}
        page2 = {"total": 35, "per_page": 30, "last_page": 2, "data": [{"session": f"s{30 + i}"} for i in range(5)]}

        client = AnimePaheClient()
        client.get_episode_page = MagicMock(side_effect=[page1, page2])

        result = client.get_episode_session("sess1", 31)
        assert result == "s30"

    def test_out_of_range(self, tmp_config_dir, sample_episode_api_response):
        client = AnimePaheClient()
        client.get_episode_page = MagicMock(return_value=sample_episode_api_response)

        result = client.get_episode_session("sess1", 999)
        assert result is None

    def test_zero(self, tmp_config_dir, sample_episode_api_response):
        client = AnimePaheClient()
        client.get_episode_page = MagicMock(return_value=sample_episode_api_response)

        result = client.get_episode_session("sess1", 0)
        assert result is None

    def test_page_fetch_fails(self, tmp_config_dir):
        client = AnimePaheClient()
        client.get_episode_page = MagicMock(return_value=None)

        result = client.get_episode_session("sess1", 1)
        assert result is None


class TestGetSources:
    def test_extracts_links(self, tmp_config_dir, mock_playwright):
        mock_playwright["page"].eval_on_selector_all.return_value = [
            {"href": "https://pahe.win/aaa", "text": "SubsPlease · 1080p (250MB)"},
            {"href": "https://pahe.win/bbb", "text": "Judas · 720p eng (120MB)"},
        ]

        client = AnimePaheClient()
        client._pw_context = mock_playwright["context"]
        client._base_url = "https://animepahe.si"

        sources = client.get_sources("anime_sess", "ep_sess")
        assert len(sources) == 2
        assert sources[0].quality == "1080p"
        assert sources[0].audio == "jpn"
        assert sources[0].fansub == "SubsPlease"
        assert sources[0].size == "250MB"
        assert sources[1].audio == "eng"

    def test_empty(self, tmp_config_dir, mock_playwright):
        mock_playwright["page"].eval_on_selector_all.return_value = []

        client = AnimePaheClient()
        client._pw_context = mock_playwright["context"]
        client._base_url = "https://animepahe.si"

        sources = client.get_sources("anime_sess", "ep_sess")
        assert sources == []


class TestClose:
    def test_cleans_up(self, tmp_config_dir):
        client = AnimePaheClient()
        mock_ctx = MagicMock()
        mock_pw = MagicMock()
        client._pw_context = mock_ctx
        client._pw = mock_pw

        client.close()
        mock_ctx.close.assert_called_once()
        mock_pw.stop.assert_called_once()

    def test_handles_exceptions(self, tmp_config_dir):
        client = AnimePaheClient()
        mock_ctx = MagicMock()
        mock_ctx.close.side_effect = Exception("already closed")
        mock_pw = MagicMock()
        client._pw_context = mock_ctx
        client._pw = mock_pw

        # Should not raise
        client.close()
        mock_pw.stop.assert_called_once()

    def test_no_context(self, tmp_config_dir):
        client = AnimePaheClient()
        # Should not crash
        client.close()
