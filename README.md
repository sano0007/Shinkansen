# anime-pahe-dl

Fast, reliable anime downloader for [AnimePahe](https://animepahe.si).

## Installation

```bash
pip install -e .
```

## Usage

```bash
# Search for anime
anime-dl search "naruto"

# Interactive download (search + download in one flow)
anime-dl get "naruto"

# Download episodes
anime-dl download <session> --episode 1 --quality 720

# Download all episodes
anime-dl download <session> --all

# Show history
anime-dl history
```

## Features

- Fast downloads with HTTP-first approach
- Cloudflare bypass with Playwright fallback
- Multiple quality options
- Download history tracking
- Rich terminal UI
