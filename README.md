# 🚅 Shinkansen - Anime Downloader (anime-pahe-dl)

> **A blazing-fast, fully interactive terminal application for downloading anime.**

<p align="left">
  <img src="demo.gif" alt="Shinkansen TUI Demo" width="700" height="450" >
</p>

`Shinkansen` is a powerful Terminal User Interface (TUI) that lets you search and batch-download anime directly
from [AnimePahe](https://animepahe.si).
It natively bypasses Cloudflare, fetches episodes in true parallel, and features an elegant interactive full-width
menu right in your terminal.

---

## Quick Start (1-Command Install)

For the absolute easiest setup across Mac/Linux/Windows, install the tool globally in its own isolated environment
using [pipx](https://pipx.pypa.io/) (the modern standard for Python CLIs):

```bash
# Safely install globally using the official PyPI release (Zero Configuration!)
pipx install shinkansen-cli
```

## 🎮 Usage

### Interactive Mode (Recommended)

Simply type the root command anywhere in your terminal to launch the pure TUI experience:
```bash
shinkansen
```

This launches a beautiful, dynamically-rendered menu offering Search & Download, Library browsing, History tracking, and
recursive Settings management—all fully navigable via your arrow keys!


### Optional: aria2c (for faster multi-connection downloads)

For accelerated downloads with multiple connections, install aria2:

- **macOS**: `brew install aria2`
- **Ubuntu/Debian**: `sudo apt install aria2`
- **Windows**: `winget install aria2`

Then set it as your download backend:
```bash
shinkansen config set download_backend aria2c
```


### Advanced Command-Line Execution

```bash
# Interactive download directly from a query
shinkansen get "naruto"

# Search for anime
shinkansen search "naruto"

# List episodes (use --page to paginate, 0=all)
shinkansen episodes <session>
shinkansen episodes <session> --page 2

# Download a single episode
shinkansen download <session> --episode 1 --quality 720 --name "Naruto"

# Download a range of episodes
shinkansen download <session> --range 1-12 --quality 1080

# Download all episodes
shinkansen download <session> --all

# Override parallel workers (default: from config)
shinkansen download <session> --all --workers 5

# Show available sources for an episode
shinkansen sources <session> 1

# View download history
shinkansen history

# Browse downloaded anime library
shinkansen library

# Find anime in library
shinkansen find "naruto"

# Manage configuration
shinkansen config show
shinkansen config set quality 720
shinkansen config set create_folder true

# Install/update Playwright Chromium browser
shinkansen setup

# View and clear download history
shinkansen history
shinkansen history --clear

# Enable debug logging
shinkansen --verbose get "naruto"
```

## Features

- **HTTP-first approach** -- tries direct API calls before launching a browser, for speed
- **Cloudflare bypass** -- Playwright fallback when Cloudflare blocks HTTP requests
- **Persistent cookie caching** -- saves Cloudflare session cookies to disk (`~/.shinkansen/cookies.json`) so subsequent
  runs skip the challenge entirely (25-min TTL)
- **Parallel batch downloads** -- spawns multiple Playwright browser instances (`prepare_workers`, default 3) to prepare
  episodes in true parallel, with a separate download thread pool (`max_downloads`, default 5); 200 episodes drops from
  ~5 hours to ~2 hours
- **Pipelined downloading** -- each episode's download starts the moment it's resolved, overlapping Playwright prep
  with file I/O
- **Multiple quality options** -- select 360p, 480p, 720p, 1080p, best, or worst
- **Sub/Dub preference** -- choose between Japanese audio with subtitles or English dub
- **Download resume** -- partial downloads are resumed automatically via HTTP Range headers
- **Episode ranges** -- download specific episodes (`--range 1-12`), comma-separated (`--range 1,3,5-7`), or all (
  `--all`)
- **Download history** -- tracks every download with anime name, episode, quality, and date
- **Anime library** -- browse and search your downloaded collection
- **Config system** -- persistent settings at `~/.shinkansen/config.json` (default quality, output dir, folder creation,
  etc.)
- **Interactive TUI** -- run `shinkansen` to access a beautiful arrow-key navigable main menu routing your entire
  application
- **Automatic Retries** -- any episodes that timeout or fail directly prompt you to instantly retry them at the end of
  the batch
- **Rich terminal UI** -- Claude-style split screen welcome banners, colored output, live progress bars, tables, and
  spinners
- **File size preview** -- view episode sizes before downloading
- **Smart download skipping** -- warns about already-downloaded episodes without re-downloading
- **Graceful shutdown** -- Ctrl+C cleanly stops workers while finishing active downloads

## Architecture

```
AnimePahe API (JSON)
    |
    v
AnimePaheClient (client.py)
    - HTTP-first with Playwright fallback
    - Cookie caching for Cloudflare sessions
    - Paginated episode fetching
    |
    v
Downloader (downloader.py)
    - animepahe.si/.com/.org -> kwik.cx/kwik.si -> direct .mp4 URL
    - Split into prepare() + download_prepared() for pipelining
    - Resume support via Range headers
    - Pluggable backends: requests (default) or aria2c
    |
    v
WorkerPool (worker_pool.py)
    - N PrepareWorkers (own Playwright browser + thread each)
    - Download ThreadPoolExecutor (bounded by max_downloads)
    - Cookie sharing: Worker 0 clears Cloudflare, others load cached cookies
    - Rich Live progress display
    |
    v
CLI (cli.py)
    - Click commands with Rich output
    - History and library management
```

## Configuration

Settings are stored at `~/.shinkansen/config.json`:

| Setting              | Default     | Description                                          |
|----------------------|-------------|------------------------------------------------------|
| `default_quality`    | `best`      | Preferred video quality                              |
| `default_output`     | `downloads` | Output directory                                     |
| `auto_retry`         | `true`      | Retry failed downloads                               |
| `retry_count`        | `3`         | Number of retry attempts                             |
| `create_folder`      | `true`      | Create per-anime subfolders                          |
| `parallel_downloads` | `3`         | Number of episodes to prefetch/download concurrently |
| `download_backend`   | `requests`  | Download engine: `requests` or `aria2c`              |
| `aria2c_path`        | `aria2c`    | Path to aria2c binary (if not in `$PATH`)            |
| `aria2c_connections` | `16`        | Segments per file for aria2c (`--split`)             |
| `prepare_workers`    | `3`         | Parallel Playwright browser instances for batch prep |
| `max_downloads`      | `5`         | Max concurrent file downloads                        |

## Testing

```bash
# Install test dependencies
pip install -e ".[test]"

# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=anime_pahe_dl --cov-report=term-missing

# Run a specific test module
pytest tests/test_config.py -v
```

## 🤝 Contributing & New Anime Sources

**We want to grow!** Shinkansen currently supports downloading directly from AnimePahe, but our goal is to build a
massive ecosystem supporting multiple anime sources.

We highly encourage the open-source community to get involved:

- **Suggest new anime sites** to support by opening an Issue.
- **Fork the repository** and open Pull Requests implementing new fetchers/extractors.
- Improve the interactive TUI or squash bugs.

Please read our `CONTRIBUTING.md` guidelines to easily get started. Let's build the ultimate anime CLI together!

## 💖 Support

If you love the blinding speed of Shinkansen and want to say thanks for the hours saved, you can buy me a coffee! It
goes a long way in keeping the project alive and well-maintained.

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/sanoo)

## Requirements

- Python >= 3.9
- [Playwright](https://playwright.dev/python/) (Chromium) -- for Cloudflare bypass and source extraction
- [Click](https://click.palletsprojects.com/) -- CLI framework
- [Rich](https://rich.readthedocs.io/) -- terminal UI
- [requests](https://requests.readthedocs.io/) -- HTTP client
- [tqdm](https://tqdm.github.io/) -- progress bars
- [aria2](https://aria2.github.io/) (optional) -- for faster multi-connection downloads
