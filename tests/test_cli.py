"""Tests for anime_pahe_dl.cli module."""

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from anime_pahe_dl.cli import (
    add_to_history,
    cli,
    load_history,
    parse_range,
    save_history,
    select_source,
)
from anime_pahe_dl.client import Anime, Episode, Source


# ── parse_range tests ────────────────────────────────────────────


class TestParseRange:
    def test_simple_range(self):
        assert parse_range("1-5") == [1, 2, 3, 4, 5]

    def test_comma_separated(self):
        assert parse_range("1,3,5") == [1, 3, 5]

    def test_mixed(self):
        assert parse_range("1-3,7,10-12") == [1, 2, 3, 7, 10, 11, 12]

    def test_single(self):
        assert parse_range("42") == [42]

    def test_deduplicates(self):
        assert parse_range("1-3,2-4") == [1, 2, 3, 4]

    def test_whitespace(self):
        assert parse_range("1 - 3, 5") == [1, 2, 3, 5]


# ── select_source tests ─────────────────────────────────────────


class TestSelectSource:
    def test_best_quality(self, sample_sources):
        source = select_source(sample_sources, "best")
        assert source.quality == "1080p"

    def test_worst_quality(self, sample_sources):
        source = select_source(sample_sources, "worst")
        assert source.quality == "360p"

    def test_exact_match(self, sample_sources):
        source = select_source(sample_sources, "720")
        assert source.quality == "720p"

    def test_exact_match_with_p(self, sample_sources):
        source = select_source(sample_sources, "720p")
        assert source.quality == "720p"

    def test_closest_match(self):
        sources = [
            Source(url="a", quality="360p", audio="jpn"),
            Source(url="b", quality="1080p", audio="jpn"),
        ]
        # 720 is closer to 1080 (diff 360) than to 360 (diff 360) — tie, but 1080 or 360 returned
        source = select_source(sources, "720")
        assert source.quality in ("360p", "1080p")

    def test_prefer_dub(self, sample_sources):
        source = select_source(sample_sources, "best", prefer_dub=True)
        assert source.is_dub is True
        assert source.quality == "720p"  # only eng source

    def test_dub_fallback_to_sub(self):
        sources = [
            Source(url="a", quality="1080p", audio="jpn"),
        ]
        # No dub available, should fall back to jpn
        source = select_source(sources, "best", prefer_dub=True)
        assert source.quality == "1080p"

    def test_empty_sources(self):
        assert select_source([], "best") is None


# ── History tests ────────────────────────────────────────────────


class TestHistory:
    def test_load_no_file(self, tmp_config_dir):
        assert load_history() == []

    def test_load_valid(self, tmp_config_dir):
        data = [{"anime": "Naruto", "episode": 1}]
        (tmp_config_dir / "history.json").write_text(json.dumps(data))
        assert load_history() == data

    def test_load_corrupt(self, tmp_config_dir):
        (tmp_config_dir / "history.json").write_text("{bad json!!")
        assert load_history() == []

    def test_save(self, tmp_config_dir):
        data = [{"anime": "Bleach", "episode": 1}]
        save_history(data)
        path = tmp_config_dir / "history.json"
        assert path.exists()
        assert json.loads(path.read_text()) == data

    def test_add_to_history(self, tmp_config_dir):
        add_to_history("Naruto", 1, "720p", "/path/to/file.mp4")
        history = load_history()
        assert len(history) == 1
        assert history[0]["anime"] == "Naruto"
        assert history[0]["episode"] == 1
        assert history[0]["quality"] == "720p"
        assert "date" in history[0]

    def test_add_multiple(self, tmp_config_dir):
        add_to_history("Naruto", 1, "720p", "f1.mp4")
        add_to_history("Naruto", 2, "720p", "f2.mp4")
        history = load_history()
        assert len(history) == 2


# ── CLI command tests ────────────────────────────────────────────


def _make_mock_client(
    search_results=None, episodes=None, sources=None, episode_session="ep_sess"
):
    """Build a mock AnimePaheClient with configurable return values."""
    mock = MagicMock()
    mock.search.return_value = search_results or []
    mock.get_episodes.return_value = episodes or []
    mock.get_episode_session.return_value = episode_session
    mock.get_sources.return_value = sources or []
    mock._pw_context = None
    return mock


def _make_mock_downloader(download_result="/path/to/file.mp4"):
    """Build a mock Downloader."""
    mock = MagicMock()
    mock.download.return_value = download_result
    mock.prepare.return_value = MagicMock()
    mock.download_prepared.return_value = download_result
    return mock


class TestCliSearch:
    def test_success(self, tmp_config_dir):
        runner = CliRunner()
        mock_client = _make_mock_client(
            search_results=[
                Anime(
                    id=1,
                    session="s1",
                    title="Naruto Shippuden",
                    episodes=500,
                    status="Completed",
                    year="2007",
                ),
            ]
        )

        with patch("anime_pahe_dl.cli.get_client", return_value=mock_client):
            result = runner.invoke(cli, ["search", "naruto"])

        assert result.exit_code == 0
        assert "Naruto Shippuden" in result.output

    def test_no_results(self, tmp_config_dir):
        runner = CliRunner()
        mock_client = _make_mock_client(search_results=[])

        with patch("anime_pahe_dl.cli.get_client", return_value=mock_client):
            result = runner.invoke(cli, ["search", "zzzznonexistent"])

        assert result.exit_code == 0
        assert "No results found" in result.output


class TestCliEpisodes:
    def test_all_episodes(self, tmp_config_dir):
        runner = CliRunner()
        mock_client = _make_mock_client(
            episodes=[
                Episode(number=1, session="e1"),
                Episode(number=2, session="e2", filler=True),
            ]
        )

        with patch("anime_pahe_dl.cli.get_client", return_value=mock_client):
            result = runner.invoke(cli, ["episodes", "test_session"])

        assert result.exit_code == 0
        assert "2 episodes" in result.output

    def test_paged(self, tmp_config_dir):
        runner = CliRunner()
        mock_client = _make_mock_client()
        mock_client.get_episode_page.return_value = {
            "total": 50,
            "last_page": 2,
            "data": [{"episode": 1, "session": "e1", "filler": 0}],
        }

        with patch("anime_pahe_dl.cli.get_client", return_value=mock_client):
            result = runner.invoke(cli, ["episodes", "test_session", "--page", "1"])

        assert result.exit_code == 0
        assert "page 1/2" in result.output


class TestCliSources:
    def test_shows_sources(self, tmp_config_dir):
        runner = CliRunner()
        mock_client = _make_mock_client(
            episode_session="ep1",
            sources=[
                Source(
                    url="https://pahe.win/a",
                    quality="1080p",
                    audio="jpn",
                    fansub="SubsPlease",
                    size="250MB",
                ),
            ],
        )

        with patch("anime_pahe_dl.cli.get_client", return_value=mock_client):
            result = runner.invoke(cli, ["sources", "test_session", "1"])

        assert result.exit_code == 0
        assert "1080p" in result.output

    def test_episode_not_found(self, tmp_config_dir):
        runner = CliRunner()
        mock_client = _make_mock_client(episode_session=None)

        with patch("anime_pahe_dl.cli.get_client", return_value=mock_client):
            result = runner.invoke(cli, ["sources", "test_session", "999"])

        assert result.exit_code == 0
        assert "not found" in result.output


class TestCliDownload:
    def test_no_option(self, tmp_config_dir):
        runner = CliRunner()
        mock_client = _make_mock_client(
            episodes=[Episode(number=1, session="e1")],
        )

        with patch("anime_pahe_dl.cli.get_client", return_value=mock_client):
            with patch(
                "anime_pahe_dl.cli.get_downloader", return_value=_make_mock_downloader()
            ):
                result = runner.invoke(cli, ["download", "test_session"])

        assert result.exit_code == 0
        assert "Specify --episode, --range, or --all" in result.output

    def test_single_episode(self, tmp_config_dir):
        runner = CliRunner()
        mock_client = _make_mock_client(
            episodes=[Episode(number=1, session="e1")],
            sources=[Source(url="https://pahe.win/a", quality="720p", audio="jpn")],
        )
        mock_pool = MagicMock()
        mock_pool.run.return_value = (1, 0)

        with patch("anime_pahe_dl.cli.get_client", return_value=mock_client):
            with patch("anime_pahe_dl.cli.WorkerPool", return_value=mock_pool):
                result = runner.invoke(
                    cli,
                    [
                        "download",
                        "test_session",
                        "--episode",
                        "1",
                        "--name",
                        "TestAnime",
                    ],
                )

        assert result.exit_code == 0
        mock_pool.run.assert_called_once()

    def test_range(self, tmp_config_dir):
        runner = CliRunner()
        mock_client = _make_mock_client(
            episodes=[Episode(number=i, session=f"e{i}") for i in range(1, 4)],
            sources=[Source(url="https://pahe.win/a", quality="720p", audio="jpn")],
        )
        mock_pool = MagicMock()
        mock_pool.run.return_value = (3, 0)

        with patch("anime_pahe_dl.cli.get_client", return_value=mock_client):
            with patch("anime_pahe_dl.cli.WorkerPool", return_value=mock_pool):
                result = runner.invoke(
                    cli,
                    [
                        "download",
                        "test_session",
                        "--range",
                        "1-3",
                        "--name",
                        "TestAnime",
                    ],
                )

        assert result.exit_code == 0
        mock_pool.run.assert_called_once()


class TestCliConfig:
    def test_show(self, tmp_config_dir):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "show"])
        assert result.exit_code == 0
        assert "default_quality" in result.output

    def test_set_valid(self, tmp_config_dir):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "retry_count", "5"])
        assert result.exit_code == 0
        assert "Set retry_count = 5" in result.output

    def test_set_invalid_key(self, tmp_config_dir):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "nonexistent_key", "val"])
        assert result.exit_code == 0
        assert "Unknown config key" in result.output

    def test_set_boolean(self, tmp_config_dir):
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "auto_retry", "false"])
        assert result.exit_code == 0
        assert "Set auto_retry = False" in result.output


class TestCliHistory:
    def test_empty(self, tmp_config_dir):
        runner = CliRunner()
        result = runner.invoke(cli, ["history"])
        assert result.exit_code == 0
        assert "No download history" in result.output

    def test_with_data(self, tmp_config_dir):
        history = [
            {
                "anime": "Naruto",
                "episode": 1,
                "quality": "720p",
                "file": "f.mp4",
                "date": "2024-01-01T00:00:00",
            },
        ]
        (tmp_config_dir / "history.json").write_text(json.dumps(history))

        runner = CliRunner()
        result = runner.invoke(cli, ["history"])
        assert result.exit_code == 0
        assert "Naruto" in result.output


class TestCliLibrary:
    def test_groups_by_anime(self, tmp_config_dir):
        history = [
            {
                "anime": "Naruto",
                "episode": 1,
                "quality": "720p",
                "file": "dl/f1.mp4",
                "date": "2024-01-01T00:00:00",
            },
            {
                "anime": "Naruto",
                "episode": 2,
                "quality": "720p",
                "file": "dl/f2.mp4",
                "date": "2024-01-01T00:00:00",
            },
            {
                "anime": "Bleach",
                "episode": 1,
                "quality": "1080p",
                "file": "dl/b1.mp4",
                "date": "2024-01-01T00:00:00",
            },
        ]
        (tmp_config_dir / "history.json").write_text(json.dumps(history))

        runner = CliRunner()
        result = runner.invoke(cli, ["library"])
        assert result.exit_code == 0
        assert "Naruto" in result.output
        assert "Bleach" in result.output

    def test_empty(self, tmp_config_dir):
        runner = CliRunner()
        result = runner.invoke(cli, ["library"])
        assert result.exit_code == 0
        assert "No downloads" in result.output


class TestCliFind:
    def test_matches(self, tmp_config_dir):
        history = [
            {
                "anime": "Naruto",
                "episode": 1,
                "quality": "720p",
                "file": "f1.mp4",
                "date": "2024-01-01T00:00:00",
            },
            {
                "anime": "Bleach",
                "episode": 1,
                "quality": "720p",
                "file": "f2.mp4",
                "date": "2024-01-01T00:00:00",
            },
        ]
        (tmp_config_dir / "history.json").write_text(json.dumps(history))

        runner = CliRunner()
        result = runner.invoke(cli, ["find", "naruto"])
        assert result.exit_code == 0
        assert "Naruto" in result.output

    def test_no_match(self, tmp_config_dir):
        history = [
            {
                "anime": "Naruto",
                "episode": 1,
                "quality": "720p",
                "file": "f.mp4",
                "date": "2024-01-01T00:00:00",
            },
        ]
        (tmp_config_dir / "history.json").write_text(json.dumps(history))

        runner = CliRunner()
        result = runner.invoke(cli, ["find", "zzzznotfound"])
        assert result.exit_code == 0
        assert "No matches" in result.output

    def test_empty_history(self, tmp_config_dir):
        runner = CliRunner()
        result = runner.invoke(cli, ["find", "anything"])
        assert result.exit_code == 0
        assert "No downloads" in result.output


class TestCliSetup:
    def test_runs_playwright_install(self, tmp_config_dir):
        runner = CliRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = runner.invoke(cli, ["setup"])

        assert result.exit_code == 0
        assert "Setup complete" in result.output
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "playwright" in call_args
        assert "install" in call_args
        assert "chromium" in call_args

    def test_setup_failure(self, tmp_config_dir):
        runner = CliRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = runner.invoke(cli, ["setup"])

        assert result.exit_code == 0
        assert "Setup failed" in result.output


class TestCliVerbose:
    def test_verbose_flag(self, tmp_config_dir):
        runner = CliRunner()
        mock_client = _make_mock_client(search_results=[])

        with patch("anime_pahe_dl.cli.get_client", return_value=mock_client):
            result = runner.invoke(cli, ["-v", "search", "test"])

        assert result.exit_code == 0
