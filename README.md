# anime-pahe-dl

Fast, reliable anime downloader for [AnimePahe](https://animepahe.si).

## Installation

```bash
pip install -e .

# Install Playwright browser (required on first run)
anime-dl setup
```

## Usage

```bash
# Interactive download (search + select + download in one flow)
anime-dl get "naruto"

# Search for anime
anime-dl search "naruto"

# List episodes
anime-dl episodes <session>

# Download a single episode
anime-dl download <session> --episode 1 --quality 720 --name "Naruto"

# Download a range of episodes
anime-dl download <session> --range 1-12 --quality 1080

# Download all episodes
anime-dl download <session> --all

# Override parallel workers (default: from config)
anime-dl download <session> --all --workers 5

# Show available sources for an episode
anime-dl sources <session> 1

# View download history
anime-dl history

# Browse downloaded anime library
anime-dl library

# Find anime in library
anime-dl find "naruto"

# Manage configuration
anime-dl config show
anime-dl config set quality 720
anime-dl config set create_folder true
```

## Features

- **HTTP-first approach** -- tries direct API calls before launching a browser, for speed
- **Cloudflare bypass** -- Playwright fallback when Cloudflare blocks HTTP requests
- **Persistent cookie caching** -- saves Cloudflare session cookies to disk (`~/.anime-dl/cookies.json`) so subsequent
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
- **Config system** -- persistent settings at `~/.anime-dl/config.json` (default quality, output dir, folder creation,
  etc.)
- **Rich terminal UI** -- colored output, progress bars, tables, and status spinners

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
    - pahe.win -> kwik.cx -> direct .mp4 URL
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

Settings are stored at `~/.anime-dl/config.json`:

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

## Requirements

- Python >= 3.9
- [Playwright](https://playwright.dev/python/) (Chromium) -- for Cloudflare bypass and source extraction
- [Click](https://click.palletsprojects.com/) -- CLI framework
- [Rich](https://rich.readthedocs.io/) -- terminal UI
- [requests](https://requests.readthedocs.io/) -- HTTP client
- [tqdm](https://tqdm.github.io/) -- progress bars
