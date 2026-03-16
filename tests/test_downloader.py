"""Tests for anime_pahe_dl.downloader module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from anime_pahe_dl.downloader import Downloader, PreparedDownload, safe_filename


# ── safe_filename tests ──────────────────────────────────────────


class TestSafeFilename:
    def test_basic(self):
        assert safe_filename("Naruto", 1, "720p") == "Naruto_Ep01_720p.mp4"

    def test_special_chars(self):
        result = safe_filename("Attack on Titan: Final Season Part 2", 5, "1080p")
        assert result == "Attack_on_Titan_Final_Season_Part_2_Ep05_1080p.mp4"

    def test_quality_without_p(self):
        result = safe_filename("Anime", 1, "720")
        assert result == "Anime_Ep01_720p.mp4"

    def test_episode_padding_single_digit(self):
        assert "Ep01" in safe_filename("X", 1, "720p")

    def test_episode_padding_double_digit(self):
        assert "Ep10" in safe_filename("X", 10, "720p")

    def test_episode_padding_triple_digit(self):
        assert "Ep100" in safe_filename("X", 100, "720p")

    def test_empty_name(self):
        result = safe_filename("", 1, "720p")
        assert result == "Anime_Ep01_720p.mp4"

    @pytest.mark.parametrize(
        "name,expected_slug",
        [
            ("!!!", "Anime"),
            ("One Piece", "One_Piece"),
            ("Re:Zero", "Re_Zero"),
            ("  spaces  ", "spaces"),
            ("under_score", "under_score"),
        ],
    )
    def test_name_sanitization(self, name, expected_slug):
        result = safe_filename(name, 1, "720p")
        assert result == f"{expected_slug}_Ep01_720p.mp4"


# ── Downloader init/lifecycle ────────────────────────────────────


class TestDownloaderInit:
    def test_creates_output_dir(self, tmp_path):
        new_dir = tmp_path / "new_output"
        dl = Downloader(str(new_dir))
        assert new_dir.exists()
        assert new_dir.is_dir()

    def test_set_playwright_context(self, tmp_path):
        dl = Downloader(str(tmp_path))
        mock_ctx = MagicMock()
        dl.set_playwright_context(mock_ctx)
        assert dl._pw_context is mock_ctx

    def test_close_shared_context(self, tmp_path):
        dl = Downloader(str(tmp_path))
        dl._owns_pw = False
        mock_ctx = MagicMock()
        dl._pw_context = mock_ctx
        dl.close()
        # Shared context — should NOT close it
        mock_ctx.close.assert_not_called()

    def test_close_owned_context(self, tmp_path):
        dl = Downloader(str(tmp_path))
        dl._owns_pw = True
        mock_ctx = MagicMock()
        mock_pw = MagicMock()
        dl._pw_context = mock_ctx
        dl._pw = mock_pw
        dl.close()
        mock_ctx.close.assert_called_once()
        mock_pw.stop.assert_called_once()


# ── _resolve_pahewin tests ───────────────────────────────────────


class TestResolvePahewin:
    def test_redirect_anchor(self, tmp_path, mock_playwright):
        mock_playwright["page"].eval_on_selector.return_value = "https://kwik.cx/f/abc123"

        dl = Downloader(str(tmp_path))
        dl._pw_context = mock_playwright["context"]

        result = dl._resolve_pahewin("https://pahe.win/xxx")
        assert result == "https://kwik.cx/f/abc123"

    def test_regex_fallback(self, tmp_path, mock_playwright):
        # wait_for_function raises (redirect anchor method fails)
        mock_playwright["page"].wait_for_function.side_effect = Exception("timeout")
        # But page HTML contains the kwik URL
        mock_playwright["page"].content.return_value = (
            '<html><body>Link: kwik.cx/f/def456</body></html>'
        )

        dl = Downloader(str(tmp_path))
        dl._pw_context = mock_playwright["context"]

        result = dl._resolve_pahewin("https://pahe.win/xxx")
        assert result == "https://kwik.cx/f/def456"

    def test_both_fail(self, tmp_path, mock_playwright):
        mock_playwright["page"].wait_for_function.side_effect = Exception("timeout")
        mock_playwright["page"].content.return_value = "<html>no kwik link</html>"

        dl = Downloader(str(tmp_path))
        dl._pw_context = mock_playwright["context"]

        result = dl._resolve_pahewin("https://pahe.win/xxx")
        assert result is None

    def test_navigation_error(self, tmp_path, mock_playwright):
        mock_playwright["page"].goto.side_effect = Exception("net::ERR_CONNECTION_REFUSED")

        dl = Downloader(str(tmp_path))
        dl._pw_context = mock_playwright["context"]

        result = dl._resolve_pahewin("https://pahe.win/xxx")
        assert result is None


# ── _extract_kwik_token tests ────────────────────────────────────


class TestExtractKwikToken:
    def test_success(self, tmp_path, mock_playwright):
        mock_hidden = MagicMock()
        mock_hidden.get_attribute.return_value = "csrf_token_123"

        mock_form = MagicMock()
        mock_form.query_selector.return_value = mock_hidden

        mock_playwright["page"].query_selector.return_value = mock_form

        dl = Downloader(str(tmp_path))
        dl._pw_context = mock_playwright["context"]

        result = dl._extract_kwik_token("https://kwik.cx/f/abc")
        assert result is not None
        assert result["token"] == "csrf_token_123"
        assert result["url"] == "https://kwik.cx/f/abc"
        assert "cookies" in result
        assert "user_agent" in result

    def test_no_form(self, tmp_path, mock_playwright):
        mock_playwright["page"].query_selector.return_value = None

        dl = Downloader(str(tmp_path))
        dl._pw_context = mock_playwright["context"]

        result = dl._extract_kwik_token("https://kwik.cx/f/abc")
        assert result is None

    def test_no_hidden_input(self, tmp_path, mock_playwright):
        mock_form = MagicMock()
        mock_form.query_selector.return_value = None
        mock_playwright["page"].query_selector.return_value = mock_form

        dl = Downloader(str(tmp_path))
        dl._pw_context = mock_playwright["context"]

        result = dl._extract_kwik_token("https://kwik.cx/f/abc")
        assert result is None

    def test_empty_token(self, tmp_path, mock_playwright):
        mock_hidden = MagicMock()
        mock_hidden.get_attribute.return_value = ""

        mock_form = MagicMock()
        mock_form.query_selector.return_value = mock_hidden
        mock_playwright["page"].query_selector.return_value = mock_form

        dl = Downloader(str(tmp_path))
        dl._pw_context = mock_playwright["context"]

        result = dl._extract_kwik_token("https://kwik.cx/f/abc")
        assert result is None


# ── _get_video_url tests ─────────────────────────────────────────


class TestGetVideoUrl:
    def _make_kwik_info(self):
        return {
            "token": "csrf_tok",
            "cookies": [{"name": "cf", "value": "v", "domain": "kwik.cx"}],
            "user_agent": "Mozilla/5.0 Test",
            "url": "https://kwik.cx/f/abc123",
        }

    def test_redirect_302(self, tmp_path):
        dl = Downloader(str(tmp_path))

        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {"Location": "https://cdn.example.com/video.mp4"}

        with patch("anime_pahe_dl.downloader._setup_session") as mock_setup:
            mock_session = MagicMock()
            mock_session.post.return_value = mock_resp
            mock_setup.return_value = mock_session

            result = dl._get_video_url(self._make_kwik_info())

        assert result is not None
        video_url, headers = result
        assert video_url == "https://cdn.example.com/video.mp4"
        assert "Referer" in headers

    def test_redirect_301(self, tmp_path):
        dl = Downloader(str(tmp_path))

        mock_resp = MagicMock()
        mock_resp.status_code = 301
        mock_resp.headers = {"Location": "https://cdn.example.com/video.mp4"}

        with patch("anime_pahe_dl.downloader._setup_session") as mock_setup:
            mock_session = MagicMock()
            mock_session.post.return_value = mock_resp
            mock_setup.return_value = mock_session

            result = dl._get_video_url(self._make_kwik_info())

        assert result is not None

    def test_200_with_mp4_in_body(self, tmp_path):
        dl = Downloader(str(tmp_path))

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = 'var url = "https://cdn.example.com/stream.mp4?token=x";'

        with patch("anime_pahe_dl.downloader._setup_session") as mock_setup:
            mock_session = MagicMock()
            mock_session.post.return_value = mock_resp
            mock_setup.return_value = mock_session

            result = dl._get_video_url(self._make_kwik_info())

        assert result is not None
        video_url, _ = result
        assert "stream.mp4" in video_url

    def test_403_forbidden(self, tmp_path):
        dl = Downloader(str(tmp_path))

        mock_resp = MagicMock()
        mock_resp.status_code = 403

        with patch("anime_pahe_dl.downloader._setup_session") as mock_setup:
            mock_session = MagicMock()
            mock_session.post.return_value = mock_resp
            mock_setup.return_value = mock_session

            result = dl._get_video_url(self._make_kwik_info())

        assert result is None

    def test_no_location_header(self, tmp_path):
        dl = Downloader(str(tmp_path))

        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {}  # No Location

        with patch("anime_pahe_dl.downloader._setup_session") as mock_setup:
            mock_session = MagicMock()
            mock_session.post.return_value = mock_resp
            mock_setup.return_value = mock_session

            result = dl._get_video_url(self._make_kwik_info())

        assert result is None

    def test_post_exception(self, tmp_path):
        dl = Downloader(str(tmp_path))

        with patch("anime_pahe_dl.downloader._setup_session") as mock_setup:
            mock_session = MagicMock()
            mock_session.post.side_effect = Exception("Connection refused")
            mock_setup.return_value = mock_session

            result = dl._get_video_url(self._make_kwik_info())

        assert result is None


# ── _download_file tests ─────────────────────────────────────────


class TestDownloadFile:
    def test_success(self, tmp_path):
        dl = Downloader(str(tmp_path))
        chunks = [b"chunk1", b"chunk2", b"chunk3"]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-length": str(sum(len(c) for c in chunks))}
        mock_resp.iter_content.return_value = iter(chunks)

        with patch("anime_pahe_dl.downloader._setup_session") as mock_setup:
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_setup.return_value = mock_session

            result = dl._download_file(
                "https://cdn.example.com/video.mp4",
                {"User-Agent": "Test"},
                "test_video.mp4",
                "Ep 1",
            )

        assert result is not None
        out_file = Path(result)
        assert out_file.exists()
        assert out_file.read_bytes() == b"chunk1chunk2chunk3"

    def test_resume(self, tmp_path):
        dl = Downloader(str(tmp_path))
        # Create a partial file
        partial = tmp_path / "resume_test.mp4"
        partial.write_bytes(b"partial_data")
        partial_size = partial.stat().st_size

        mock_resp = MagicMock()
        mock_resp.status_code = 206
        mock_resp.headers = {"content-length": "10"}
        mock_resp.iter_content.return_value = iter([b"more_data!"])

        with patch("anime_pahe_dl.downloader._setup_session") as mock_setup:
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_setup.return_value = mock_session

            result = dl._download_file(
                "https://cdn.example.com/video.mp4",
                {"User-Agent": "Test"},
                "resume_test.mp4",
                "Ep 1",
            )

        assert result is not None
        # File should have original + new data
        content = Path(result).read_bytes()
        assert content == b"partial_datamore_data!"

        # Verify Range header was set
        get_call = mock_session.get.call_args
        headers = get_call.kwargs.get("headers", get_call[1].get("headers", {}))
        assert f"bytes={partial_size}-" in headers.get("Range", "")

    def test_already_complete_416(self, tmp_path):
        dl = Downloader(str(tmp_path))
        complete = tmp_path / "complete.mp4"
        complete.write_bytes(b"full_video_data")

        mock_resp = MagicMock()
        mock_resp.status_code = 416

        with patch("anime_pahe_dl.downloader._setup_session") as mock_setup:
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_setup.return_value = mock_session

            result = dl._download_file(
                "https://cdn.example.com/video.mp4",
                {"User-Agent": "Test"},
                "complete.mp4",
                "Ep 1",
            )

        assert result is not None
        assert Path(result).read_bytes() == b"full_video_data"

    def test_http_error(self, tmp_path):
        dl = Downloader(str(tmp_path))

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("anime_pahe_dl.downloader._setup_session") as mock_setup:
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_setup.return_value = mock_session

            result = dl._download_file(
                "https://cdn.example.com/video.mp4",
                {"User-Agent": "Test"},
                "error.mp4",
                "Ep 1",
            )

        assert result is None


# ── prepare / download_prepared tests ────────────────────────────


class TestPrepare:
    def test_pahewin_url(self, tmp_path):
        dl = Downloader(str(tmp_path))
        dl._resolve_pahewin = MagicMock(return_value="https://kwik.cx/f/abc")
        dl._extract_kwik_token = MagicMock(
            return_value={"token": "t", "cookies": [], "user_agent": "ua", "url": "https://kwik.cx/f/abc"})
        dl._get_video_url = MagicMock(return_value=("https://cdn.example.com/v.mp4", {"User-Agent": "ua"}))

        result = dl.prepare("https://pahe.win/xxx")
        assert isinstance(result, PreparedDownload)
        assert result.video_url == "https://cdn.example.com/v.mp4"
        assert result.kwik_url == "https://kwik.cx/f/abc"

    def test_kwik_url_directly(self, tmp_path):
        dl = Downloader(str(tmp_path))
        dl._resolve_pahewin = MagicMock()  # Should NOT be called
        dl._extract_kwik_token = MagicMock(
            return_value={"token": "t", "cookies": [], "user_agent": "ua", "url": "https://kwik.cx/f/abc"})
        dl._get_video_url = MagicMock(return_value=("https://cdn.example.com/v.mp4", {}))

        result = dl.prepare("https://kwik.cx/f/abc")
        assert result is not None
        dl._resolve_pahewin.assert_not_called()

    def test_unknown_url(self, tmp_path):
        dl = Downloader(str(tmp_path))
        result = dl.prepare("https://random.com/xyz")
        assert result is None

    def test_resolve_fails(self, tmp_path):
        dl = Downloader(str(tmp_path))
        dl._resolve_pahewin = MagicMock(return_value=None)
        result = dl.prepare("https://pahe.win/xxx")
        assert result is None

    def test_token_fails(self, tmp_path):
        dl = Downloader(str(tmp_path))
        dl._resolve_pahewin = MagicMock(return_value="https://kwik.cx/f/abc")
        dl._extract_kwik_token = MagicMock(return_value=None)
        result = dl.prepare("https://pahe.win/xxx")
        assert result is None

    def test_video_url_fails(self, tmp_path):
        dl = Downloader(str(tmp_path))
        dl._resolve_pahewin = MagicMock(return_value="https://kwik.cx/f/abc")
        dl._extract_kwik_token = MagicMock(return_value={"token": "t", "cookies": [], "user_agent": "ua", "url": "u"})
        dl._get_video_url = MagicMock(return_value=None)
        result = dl.prepare("https://pahe.win/xxx")
        assert result is None


class TestDownloadPrepared:
    def test_creates_anime_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("anime_pahe_dl.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("anime_pahe_dl.config.CONFIG_FILE", tmp_path / "config.json")

        dl = Downloader(str(tmp_path / "downloads"))
        prepared = PreparedDownload(
            video_url="https://cdn.example.com/v.mp4",
            headers={"User-Agent": "test"},
            kwik_url="https://kwik.cx/f/abc",
        )
        dl._download_file = MagicMock(return_value=str(tmp_path / "downloads" / "Naruto" / "Naruto_Ep01_720p.mp4"))

        result = dl.download_prepared(prepared, "Naruto", 1, "720p")
        assert result is not None

        # Verify _download_file was called with anime subfolder
        call_args = dl._download_file.call_args
        output_dir = call_args[0][4] if len(call_args[0]) > 4 else call_args.kwargs.get("output_dir")
        # The output_dir should end with the anime folder name
        assert "Naruto" in str(output_dir)

    def test_no_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("anime_pahe_dl.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("anime_pahe_dl.config.CONFIG_FILE", tmp_path / "config.json")

        # Save config with create_folder=False
        import json
        (tmp_path / "config.json").write_text(json.dumps({"create_folder": False}))

        dl = Downloader(str(tmp_path / "downloads"))
        prepared = PreparedDownload(
            video_url="https://cdn.example.com/v.mp4",
            headers={"User-Agent": "test"},
            kwik_url="https://kwik.cx/f/abc",
        )
        dl._download_file = MagicMock(return_value=str(tmp_path / "downloads" / "Anime_Ep01_720p.mp4"))

        result = dl.download_prepared(prepared, "Anime", 1, "720p")
        assert result is not None


class TestDownloadFullPipeline:
    def test_chains_prepare_and_download(self, tmp_path):
        dl = Downloader(str(tmp_path))
        prepared = PreparedDownload(
            video_url="https://cdn.example.com/v.mp4",
            headers={},
            kwik_url="https://kwik.cx/f/abc",
        )
        dl.prepare = MagicMock(return_value=prepared)
        dl.download_prepared = MagicMock(return_value="/path/to/file.mp4")

        result = dl.download("https://pahe.win/xxx", "Anime", 1, "720p")
        assert result == "/path/to/file.mp4"
        dl.prepare.assert_called_once_with("https://pahe.win/xxx")
        dl.download_prepared.assert_called_once_with(prepared, "Anime", 1, "720p")

    def test_prepare_fails(self, tmp_path):
        dl = Downloader(str(tmp_path))
        dl.prepare = MagicMock(return_value=None)

        result = dl.download("https://pahe.win/xxx")
        assert result is None
