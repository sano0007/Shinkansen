"""
CLI interface for anime-pahe-dl.

Usage:
    anime-dl search "bleach"
    anime-dl episodes <session>
    anime-dl download <session> --episode 1 --quality 1080
    anime-dl download <session> --range 1-12 --quality 720
    anime-dl download <session> --all
    anime-dl get "bleach"          # Interactive search & download
    anime-dl history               # Show download history
    anime-dl library               # Show downloaded anime
    anime-dl config show           # Show config
    anime-dl config set quality 720

Improvements:
- Shows all info (quality, size, sub/dub) before downloading
- Episode range support (--range 1-12)
- Resume support (won't re-download existing files)
- Proper quality selection with fallback
- Rich terminal output
- Download history tracking
- Interactive 'get' command for quick workflow
- Config system with anime folder creation
- Library management
"""

import atexit
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from anime_pahe_dl.client import AnimePaheClient
from anime_pahe_dl.config import load_config, set_config, get_config, DEFAULT_CONFIG
from anime_pahe_dl.downloader import Downloader
from anime_pahe_dl.worker_pool import WorkerPool, EpisodeTask

console = Console()


def _check_aria2c(prompt_install: bool = False) -> bool:
    """Return True if aria2c binary is available.

    If not found, print a helpful message. When prompt_install=True (used by
    'config set') ask the user whether to keep the setting anyway.
    """
    import shutil
    from anime_pahe_dl.config import get_config

    bin_path = get_config("aria2c_path", "aria2c")
    if shutil.which(bin_path):
        return True

    console.print(
        f"\n[bold red]aria2c not found[/bold red] (looked for [cyan]{bin_path}[/cyan])\n"
        "\nInstall it with:\n"
        "  [bold]macOS :[/bold]  brew install aria2\n"
        "  [bold]Ubuntu:[/bold]  sudo apt install aria2\n"
        "  [bold]Windows:[/bold] winget install aria2  [dim](or scoop install aria2)[/dim]\n"
        "\nThen run [cyan]anime-dl config set aria2c_path /path/to/aria2c[/cyan] if it's not in PATH."
    )

    if prompt_install:
        keep = Confirm.ask(
            "\n[yellow]Save 'download_backend = aria2c' anyway?[/yellow] "
            "(downloads will fall back to requests until aria2c is installed)",
            default=False,
        )
        return keep  # caller decides whether to persist

    return False


# Shared client instance
_client: Optional[AnimePaheClient] = None
_downloader: Optional[Downloader] = None

# History file
HISTORY_DIR = Path.home() / ".anime-dl"
HISTORY_FILE = HISTORY_DIR / "history.json"


def get_history_dir() -> Path:
    """Ensure history directory exists."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return HISTORY_DIR


def load_history() -> list[dict]:
    """Load download history from file."""
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def save_history(history: list[dict]):
    """Save download history to file."""
    get_history_dir()
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def add_to_history(anime_name: str, episode: int, quality: str, file_path: str):
    """Add a download to history."""
    history = load_history()
    history.append(
        {
            "anime": anime_name,
            "episode": episode,
            "quality": quality,
            "file": file_path,
            "date": datetime.now().isoformat(),
        }
    )
    save_history(history)


def get_client() -> AnimePaheClient:
    global _client
    if _client is None:
        _client = AnimePaheClient()
    return _client


def get_downloader(output_dir: str = "downloads") -> Downloader:
    global _downloader
    if _downloader is None:
        _downloader = Downloader(output_dir)
    # Always ensure the downloader has the client's Playwright context
    # This prevents the "Sync API inside asyncio loop" conflict
    if _client and _client._pw_context:
        _downloader.set_playwright_context(_client._pw_context)
    return _downloader


def _cleanup():
    if _client:
        _client.close()
    if _downloader:
        _downloader.close()


atexit.register(_cleanup)

# Re-export from utils for backward compatibility and test imports
from anime_pahe_dl.utils import parse_range, select_source  # noqa: F401, E402

HAS_PRINTED_BANNER = False


def _print_banner_once():
    global HAS_PRINTED_BANNER
    if not HAS_PRINTED_BANNER:
        console.print(_render_welcome_banner())
        HAS_PRINTED_BANNER = True


def _handle_interactive_main_menu(ctx):
    """Show the interactive root menu."""
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    _print_banner_once()
    while True:
        try:
            choice = inquirer.select(
                message="What would you like to do?",
                choices=[
                    Choice("search", name="🔍 Search & Download Anime"),
                    Choice("library", name="📚 View Library"),
                    Choice("history", name="🕒 View Download History"),
                    Choice("settings", name="⚙️  Settings"),
                    Choice("exit", name="❌ Exit"),
                ],
                pointer="➜",
            ).execute()
        except KeyboardInterrupt:
            break

        if choice == "search":
            try:
                query = inquirer.text(message="Search query:").execute()
                if query.strip():
                    ctx.invoke(
                        get,
                        query=query.strip(),
                        quality=None,
                        dub=None,
                        output="downloads",
                        workers=None,
                    )
            except KeyboardInterrupt:
                pass
        elif choice == "library":
            ctx.invoke(library)
            try:
                inquirer.text(message="Press Enter to return...").execute()
            except KeyboardInterrupt:
                pass
        elif choice == "history":
            ctx.invoke(history, clear=False)
            try:
                inquirer.text(message="Press Enter to return...").execute()
            except KeyboardInterrupt:
                pass
        elif choice == "settings":
            ctx.invoke(config_show)
            try:
                inquirer.text(message="Press Enter to return...").execute()
            except KeyboardInterrupt:
                pass
        elif choice == "exit":
            break


def _run_download_with_retries(pool, tasks, anime_name, output):
    """Helper to run the pool and recursively prompt for failed episode retries."""
    total_success = 0
    current_tasks = tasks

    while current_tasks:
        success, failed_tasks = pool.run(current_tasks)
        total_success += success

        if not failed_tasks:
            break

        from InquirerPy import inquirer

        try:
            retry = inquirer.confirm(
                message=f"{len(failed_tasks)} episode(s) failed. Retry them?",
                default=True,
            ).execute()
        except KeyboardInterrupt:
            retry = False

        if not retry:
            current_tasks = failed_tasks
            break

        current_tasks = failed_tasks

    failed_count = len(current_tasks) if current_tasks else 0

    console.print(
        Panel(
            f"[bold]Anime:[/bold] {anime_name}\n"
            f"[bold]Target:[/bold] {output}/\n\n"
            f"[green]✓ Downloaded: {total_success} episode(s)[/green]\n"
            + (
                f"[red]✗ Failed: {failed_count} episode(s)[/red]\n"
                if failed_count
                else ""
            )
            + "\n[dim]Enjoy watching![/dim]",
            title="✨ Download Complete ✨",
            border_style="green" if failed_count == 0 else "yellow",
            expand=False,
        )
    )


# ── CLI Commands ─────────────────────────────────────────────────


@click.group(invoke_without_command=True)
@click.pass_context
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
def cli(ctx, verbose):
    """anime-pahe-dl — Fast anime downloader for AnimePahe"""
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    if ctx.invoked_subcommand is None:
        _handle_interactive_main_menu(ctx)


@cli.command()
@click.argument("query")
def search(query):
    """Search for anime by name."""
    client = get_client()

    with console.status("[bold cyan]Searching...", spinner="dots"):
        results = client.search(query)

    if not results:
        console.print("[red]No results found.[/red]")
        console.print("[dim]Tips: check spelling, try shorter keywords[/dim]")
        return

    table = Table(title=f"Search Results for '{query}'", show_lines=True)
    table.add_column("#", style="cyan", width=4)
    table.add_column("Title", style="bold white", min_width=30)
    table.add_column("Session", style="dim", max_width=40)
    table.add_column("Episodes", style="green", justify="right")
    table.add_column("Status", style="yellow")
    table.add_column("Year", style="dim")

    for i, anime in enumerate(results, 1):
        table.add_row(
            str(i),
            anime.title,
            anime.session,
            str(anime.episodes),
            anime.status,
            anime.year,
        )

    console.print(table)
    console.print(
        "\n[dim]Use the session ID with other commands:[/dim]"
        "\n  [cyan]anime-dl episodes <session>[/cyan]"
        "\n  [cyan]anime-dl download <session> --episode 1[/cyan]"
    )


@cli.command()
@click.argument("session")
@click.option("-p", "--page", default=0, type=int, help="Page number (0=all)")
def episodes(session, page):
    """List episodes for an anime (use session from search)."""
    client = get_client()

    if page > 0:
        with console.status("[bold cyan]Fetching episodes...", spinner="dots"):
            data = client.get_episode_page(session, page)
        if not data or "data" not in data:
            console.print("[red]No episodes found.[/red]")
            return

        total = data.get("total", "?")
        last_page = data.get("last_page", 1)
        console.print(
            f"[bold]Episodes[/bold] (page {page}/{last_page}, {total} total)\n"
        )
        for ep in data["data"]:
            num = ep.get("episode", "?")
            ep_session = ep.get("session", "")
            filler = " [dim red](filler)[/dim red]" if ep.get("filler") else ""
            console.print(f"  Ep {num:>4}{filler}  [dim]{ep_session}[/dim]")
    else:
        with console.status("[bold cyan]Fetching all episodes...", spinner="dots"):
            eps = client.get_episodes(session)

        if not eps:
            console.print("[red]No episodes found.[/red]")
            return

        console.print(f"[bold]Found {len(eps)} episodes[/bold]\n")
        for ep in eps:
            filler = " [dim red](filler)[/dim red]" if ep.filler else ""
            console.print(f"  Ep {ep.number:>4}{filler}  [dim]{ep.session}[/dim]")


@cli.command()
@click.argument("session")
@click.argument("episode_num", type=int)
def sources(session, episode_num):
    """Show available download sources for an episode."""
    client = get_client()

    with console.status("[bold cyan]Getting episode info...", spinner="dots"):
        ep_session = client.get_episode_session(session, episode_num)

    if not ep_session:
        console.print(f"[red]Episode {episode_num} not found.[/red]")
        return

    with console.status("[bold cyan]Getting sources...", spinner="dots"):
        srcs = client.get_sources(session, ep_session)

    if not srcs:
        console.print("[red]No sources found.[/red]")
        return

    table = Table(title=f"Sources for Episode {episode_num}")
    table.add_column("#", style="cyan", width=4)
    table.add_column("Quality", style="green")
    table.add_column("Audio", style="yellow")
    table.add_column("Fansub", style="dim")
    table.add_column("Size", style="magenta")
    table.add_column("URL", style="dim", max_width=50)

    for i, src in enumerate(srcs, 1):
        audio_label = "🔊 ENG (Dub)" if src.is_dub else "🎌 JPN (Sub)"
        table.add_row(
            str(i), src.quality, audio_label, src.fansub, src.size, src.url[:50]
        )

    console.print(table)


@cli.command()
@click.argument("session")
@click.option("-e", "--episode", type=int, help="Single episode number")
@click.option("-r", "--range", "ep_range", help="Episode range (e.g., 1-12 or 1,3,5-7)")
@click.option("-a", "--all", "download_all", is_flag=True, help="Download all episodes")
@click.option(
    "-q", "--quality", default="best", help="Quality: 360, 480, 720, 1080, best, worst"
)
@click.option("-d", "--dub", is_flag=True, help="Prefer English dub")
@click.option("-o", "--output", default="downloads", help="Output directory")
@click.option("-n", "--name", default=None, help="Anime name (for filename)")
@click.option(
    "-w",
    "--workers",
    type=int,
    default=None,
    help="Parallel Playwright workers (default: from config)",
)
def download(
    session, episode, ep_range, download_all, quality, dub, output, name, workers
):
    """Download episodes from AnimePahe."""
    client = get_client()

    # Determine which episodes to download
    # Fetch all episodes upfront for the session map (avoids per-episode API calls)
    with console.status("[bold cyan]Fetching episode list...", spinner="dots"):
        eps = client.get_episodes(session)

    ep_session_map = {ep.number: ep.session for ep in eps}

    if episode:
        ep_numbers = [episode]
    elif ep_range:
        ep_numbers = parse_range(ep_range)
    elif download_all:
        ep_numbers = [ep.number for ep in eps]
        console.print(f"[bold]Will download {len(ep_numbers)} episodes[/bold]")
    else:
        console.print("[red]Specify --episode, --range, or --all[/red]")
        return

    anime_name = name or "Anime"

    console.print(
        Panel(
            f"[bold]{anime_name}[/bold]\n"
            f"Episodes: {ep_numbers[0]}-{ep_numbers[-1]} ({len(ep_numbers)} total)\n"
            f"Quality: {quality} | Audio: {'Dub' if dub else 'Sub'}\n"
            f"Output: {output}/",
            title="Download Plan",
            border_style="cyan",
        )
    )

    # Warn early if aria2c backend is selected but binary is missing
    if get_config("download_backend", "requests") == "aria2c":
        _check_aria2c()

    # Build task list for the worker pool
    tasks = [
        EpisodeTask(
            ep_num=ep,
            ep_session=ep_session_map[ep],
            anime_session=session,
            anime_name=anime_name,
            quality=quality,
            prefer_dub=dub,
        )
        for ep in ep_numbers
        if ep in ep_session_map
    ]

    pool = WorkerPool(
        num_workers=workers or int(get_config("prepare_workers", 3)),
        max_downloads=int(get_config("max_downloads", 5)),
        output_dir=output,
        on_complete=lambda ep, q, path: add_to_history(anime_name, ep, q, path),
    )
    _run_download_with_retries(pool, tasks, anime_name, output)


@cli.command()
def setup():
    """Install Playwright browsers (run this first!)."""
    import subprocess

    console.print("[bold]Installing Playwright Chromium...[/bold]")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=False,
    )
    if result.returncode == 0:
        console.print("[green]✓ Setup complete! You're ready to download.[/green]")
    else:
        console.print("[red]✗ Setup failed. Try: playwright install chromium[/red]")


@cli.command()
@click.option("-c", "--clear", is_flag=True, help="Clear download history")
def history(clear):
    """Show download history."""
    if clear:
        if Confirm.ask("[red]Clear all download history?[/red]", default=False):
            save_history([])
            console.print("[green]History cleared.[/green]")
        return

    history_data = load_history()
    if not history_data:
        console.print("[dim]No download history yet.[/dim]")
        console.print("[dim]Downloads will be tracked automatically.[/dim]")
        return

    table = Table(title="Download History")
    table.add_column("Anime", style="bold white")
    table.add_column("Ep", justify="right", style="cyan")
    table.add_column("Quality", style="green")
    table.add_column("Date", style="dim")
    table.add_column("File", style="dim", max_width=40)

    for item in reversed(history_data[-50:]):  # Show last 50
        date = item.get("date", "")
        if "T" in date:
            date = date.split("T")[0]
        table.add_row(
            item.get("anime", "Unknown"),
            str(item.get("episode", "?")),
            item.get("quality", "?"),
            date,
            item.get("file", "")[:40],
        )

    console.print(table)
    console.print(f"\n[dim]Total downloads: {len(history_data)}[/dim]")


def _render_welcome_banner():
    """Render a Claude-style split layout welcome banner."""
    from rich.table import Table
    from rich.panel import Panel
    from rich.align import Align
    from rich.console import Group

    # A simple but impactful logo
    ascii_art = """[bold magenta]
    ▶
  ██████
  ██████
[/bold magenta]"""

    table = Table(show_header=False, box=None, padding=(0, 2), show_edge=False)
    table.add_column("Left", justify="center", width=28)
    table.add_column("Right", justify="left")

    left = Group(
        Align.center("\n[bold white]Welcome back![/bold white]"),
        Align.center(ascii_art),
        Align.center("[dim]anime-pahe-dl v1.0[/dim]"),
        Align.center(f"[dim]~/{get_config('output_dir', 'downloads')}[/dim]"),
    )

    history = load_history()
    recent = "No recent activity"
    if history:
        recent_anime = []
        for h in reversed(history):
            if h.get("anime") and h["anime"] not in recent_anime:
                recent_anime.append(h["anime"])
            if len(recent_anime) >= 2:
                break
        recent = "\n".join(f"• {a}" for a in recent_anime)

    right = Group(
        "[dim]Tips for getting started[/dim]",
        "Run [cyan]anime-dl config show[/cyan] to overview settings",
        "Use [cyan]arrow keys[/cyan] to elegantly navigate menus\n",
        "[dim]Recent activity[/dim]",
        recent,
    )

    table.add_row(left, right)

    return Panel(
        table,
        title="[bold magenta] anime-pahe-dl [/bold magenta]",
        border_style="magenta",
        expand=False,
    )


@cli.command()
@click.argument("query")
@click.option(
    "-q",
    "--quality",
    default=None,
    help="Quality: 360, 480, 720, 1080, best (will ask if not set)",
)
@click.option(
    "-d",
    "--dub",
    is_flag=True,
    default=None,
    help="Prefer English dub (will ask if not set)",
)
@click.option("-o", "--output", default="downloads", help="Output directory")
@click.option(
    "-w",
    "--workers",
    type=int,
    default=None,
    help="Parallel Playwright workers (default: from config)",
)
def get(query, quality, dub, output, workers):
    """Interactive search and download - all in one!"""
    client = get_client()

    _print_banner_once()

    # Step 1: Search
    with console.status("[bold cyan]Searching...", spinner="dots"):
        results = client.search(query)

    if not results:
        console.print("[red]No results found.[/red]")
        return

    # Step 2: Select anime
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [
        Choice(
            i,
            name=f"{anime.title} ({anime.episodes} eps) - {anime.year} {anime.status}",
        )
        for i, anime in enumerate(results)
    ]

    try:
        selected_index = inquirer.select(
            message=f"Select anime for '{query}':",
            choices=choices,
            pointer="➜",
        ).execute()
    except KeyboardInterrupt:
        return

    selected = results[selected_index]
    session = selected.session
    anime_name = selected.title

    console.print(f"\n[green]Selected:[/green] {anime_name}")

    # Step 3: Get episodes
    with console.status("[bold cyan]Fetching episodes...", spinner="dots"):
        eps = client.get_episodes(session)

    if not eps:
        console.print("[red]No episodes found.[/red]")
        return

    console.print(f"[bold]Found {len(eps)} episodes[/bold]")

    ep_session_map = {ep.number: ep.session for ep in eps}

    # Step 4: Select episodes
    try:
        ep_choices = inquirer.text(
            message="Episodes to download (e.g., 1-5, 1,3,5 or 'all'):",
            default="all",
        ).execute()
    except KeyboardInterrupt:
        return

    if ep_choices.lower() == "all":
        ep_numbers = [ep.number for ep in eps]
    else:
        ep_numbers = parse_range(ep_choices)

    console.print(f"\n[cyan]Will download:[/cyan] episodes {ep_numbers}")

    # Step 5: Select quality (if not provided)
    if quality is None:
        try:
            quality = inquirer.select(
                message="Quality:",
                choices=["360", "480", "720", "1080", "best"],
                default="best",
                pointer="➜",
            ).execute()
        except KeyboardInterrupt:
            return

    # Step 6: Select sub/dub (if not provided)
    if dub is None:
        try:
            audio_choice = inquirer.select(
                message="Audio:",
                choices=[
                    Choice("sub", name="🎌 Japanese (Sub)"),
                    Choice("dub", name="🔊 English (Dub)"),
                ],
                default="sub",
                pointer="➜",
            ).execute()
            prefer_dub = audio_choice == "dub"
        except KeyboardInterrupt:
            return
    else:
        prefer_dub = dub

    audio_label = "Dub" if prefer_dub else "Sub"
    console.print(f"\n[cyan]Settings:[/cyan] Quality: {quality}, Audio: {audio_label}")

    # Step 7: Confirm and download
    try:
        if not inquirer.confirm(message="Start download?", default=True).execute():
            console.print("[yellow]Cancelled.[/yellow]")
            return
    except KeyboardInterrupt:
        return

    # Step 8: Parallel worker pool
    if get_config("download_backend", "requests") == "aria2c":
        _check_aria2c()

    tasks = [
        EpisodeTask(
            ep_num=ep,
            ep_session=ep_session_map[ep],
            anime_session=session,
            anime_name=anime_name,
            quality=quality,
            prefer_dub=prefer_dub,
        )
        for ep in ep_numbers
        if ep in ep_session_map
    ]

    pool = WorkerPool(
        num_workers=workers or int(get_config("prepare_workers", 3)),
        max_downloads=int(get_config("max_downloads", 5)),
        output_dir=output,
        on_complete=lambda ep, q, path: add_to_history(anime_name, ep, q, path),
    )
    _run_download_with_retries(pool, tasks, anime_name, output)


@cli.group()
def config():
    """Manage configuration."""
    pass


@config.command("show")
def config_show():
    """Show current configuration."""
    cfg = load_config()

    table = Table(title="Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")

    for key, value in cfg.items():
        table.add_row(key, str(value))

    console.print(table)
    console.print("\n[dim]Use 'anime-dl config set <key> <value>' to change[/dim]")


@config.command("set")
@click.argument("key")
@click.argument("value", required=False)
def config_set(key, value):
    """Set a config value. Usage: config set <key> <value>"""
    if key not in DEFAULT_CONFIG:
        console.print(f"[red]Unknown config key: {key}[/red]")
        console.print(f"[dim]Available: {', '.join(DEFAULT_CONFIG.keys())}[/dim]")
        return

    # Handle boolean values
    if isinstance(DEFAULT_CONFIG[key], bool):
        if value is None:
            console.print(f"[red]Value required for {key} (true/false)[/red]")
            return
        value = value.lower() in ("true", "1", "yes")
    elif isinstance(DEFAULT_CONFIG[key], int):
        if value is None:
            console.print(f"[red]Value required for {key} (number)[/red]")
            return
        value = int(value)

    # Before saving, validate aria2c availability when switching to that backend
    if key == "download_backend" and value == "aria2c":
        keep = _check_aria2c(prompt_install=True)
        if not keep:
            console.print("[yellow]Cancelled — download_backend unchanged.[/yellow]")
            return

    set_config(key, value)
    console.print(f"[green]Set {key} = {value}[/green]")


@cli.command()
def library():
    """Show downloaded anime library."""
    history = load_history()

    if not history:
        console.print("[dim]No downloads yet.[/dim]")
        return

    # Group by anime
    anime_list = {}
    for item in history:
        anime = item.get("anime", "Unknown")
        if anime not in anime_list:
            anime_list[anime] = {"episodes": set(), "quality": set(), "files": []}
        anime_list[anime]["episodes"].add(item.get("episode"))
        anime_list[anime]["quality"].add(item.get("quality"))
        anime_list[anime]["files"].append(item.get("file", ""))

    table = Table(title="Anime Library")
    table.add_column("Anime", style="bold white")
    table.add_column("Episodes", style="cyan", justify="right")
    table.add_column("Quality", style="green")
    table.add_column("Folder", style="dim", max_width=40)

    for anime, data in sorted(anime_list.items()):
        eps = len(data["episodes"])
        qual = ", ".join(sorted(data["quality"]))
        folder = Path(data["files"][0]).parent.name if data["files"] else "?"
        table.add_row(anime, str(eps), qual, folder)

    console.print(table)
    console.print(f"\n[dim]Total: {len(anime_list)} anime[/dim]")


@cli.command()
@click.argument("query")
def find(query):
    """Find anime in library by name."""
    history = load_history()

    if not history:
        console.print("[dim]No downloads yet.[/dim]")
        return

    # Find matching anime
    matches = {}
    for item in history:
        anime = item.get("anime", "")
        if query.lower() in anime.lower():
            if anime not in matches:
                matches[anime] = {"episodes": set(), "quality": set(), "files": []}
            matches[anime]["episodes"].add(item.get("episode"))
            matches[anime]["quality"].add(item.get("quality"))
            matches[anime]["files"].append(item.get("file", ""))

    if not matches:
        console.print(f"[yellow]No matches for '{query}'[/yellow]")
        return

    table = Table(title=f"Results for '{query}'")
    table.add_column("Anime", style="bold white")
    table.add_column("Episodes", style="cyan", justify="right")
    table.add_column("Quality", style="green")
    table.add_column("Folder", style="dim", max_width=50)

    for anime, data in sorted(matches.items()):
        eps = len(data["episodes"])
        qual = ", ".join(sorted(data["quality"]))
        folder = data["files"][0] if data["files"] else "?"
        table.add_row(anime, str(eps), qual, folder[:50])

    console.print(table)


if __name__ == "__main__":
    cli()
