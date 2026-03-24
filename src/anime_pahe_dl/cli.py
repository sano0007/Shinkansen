"""
CLI interface for anime-pahe-dl.

Usage:
    shinkansen search "bleach"
    shinkansen episodes <session>
    shinkansen download <session> --episode 1 --quality 1080
    shinkansen download <session> --range 1-12 --quality 720
    shinkansen download <session> --all
    shinkansen get "bleach"          # Interactive search & download
    shinkansen history               # Show download history
    shinkansen library               # Show downloaded anime
    shinkansen config show           # Show config
    shinkansen config set quality 720

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
import contextlib
import json
import logging
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
import nest_asyncio
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from anime_pahe_dl.client import AnimePaheClient
from anime_pahe_dl.config import load_config, set_config, get_config, DEFAULT_CONFIG
from anime_pahe_dl.downloader import Downloader
from anime_pahe_dl.worker_pool import WorkerPool, EpisodeTask

console = Console()
nest_asyncio.apply()


@contextlib.contextmanager
def _fun_status(action="loading"):
    """Context manager that cycles through fun messages while work happens."""
    import threading
    import itertools

    messages = {
        "search": [
            "Searching the anime multiverse",
            "Scanning AnimePahe servers",
            "Summoning search results",
            "Consulting the anime gods",
            "Cooking up results",
            "No cap, searching fr fr",
            "This search is about to be bussin",
        ],
        "episodes": [
            "Fetching episode list",
            "Counting episodes",
            "Loading the episode archive",
            "Unpacking the season",
            "Brewing episode data",
            "Slay, grabbing that episode data",
            "Lowkey fetching all the eps",
        ],
        "sources": [
            "Resolving download sources",
            "Cracking the download links",
            "Decoding stream URLs",
            "Extracting the good stuff",
            "These links are giving main character energy",
            "Fr fr finding the best source",
            "No cap, extracting the vibes",
        ],
        "loading": [
            "Loading",
            "Working on it",
            "Crunching data",
            "Almost there",
            "Warming up",
            "Lowkey cooking rn",
            "It's giving progress",
        ],
    }

    pool = messages.get(action, messages["loading"])
    # Shuffle for variety
    random.shuffle(pool)
    it = itertools.cycle(pool)

    stop_event = threading.Event()

    def rotate_status(status_obj):
        while not stop_event.wait(4.0):
            msg = next(it)
            status_obj.update(f"[bold cyan]{msg}...[/bold cyan]")

    with console.status(
        f"[bold cyan]{next(it)}...[/bold cyan]", spinner="dots"
    ) as status:
        rotator = threading.Thread(target=rotate_status, args=(status,), daemon=True)
        rotator.start()
        try:
            yield
        finally:
            stop_event.set()


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
        "\nThen run [cyan]shinkansen config set aria2c_path /path/to/aria2c[/cyan] if it's not in PATH."
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
HISTORY_DIR = Path.home() / ".shinkansen"
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
                    Choice("search", name="Search & Download"),
                    Choice("library", name="Library"),
                    Choice("history", name="Download History"),
                    Choice("settings", name="Settings"),
                    Choice("exit", name="Exit"),
                ],
                pointer="›",
            ).execute()
        except KeyboardInterrupt:
            break

        if choice == "search":
            try:
                query = inquirer.text(message="Search query:").execute()
                if query.strip():
                    import threading

                    _exc = []

                    def _run_in_clean_thread():
                        try:
                            ctx.invoke(
                                get,
                                query=query.strip(),
                                quality=None,
                                dub=None,
                                output="downloads",
                                workers=None,
                            )
                        except Exception as e:
                            _exc.append(e)

                    t = threading.Thread(target=_run_in_clean_thread)
                    t.start()
                    t.join()
                    if _exc:
                        raise _exc[0]
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
    completed_numbers = set()
    current_tasks = tasks

    while current_tasks:
        success_count, failed_tasks = pool.run(current_tasks)

        # Track which episodes actually finished successfully in THIS run
        # We assume any task NOT in failed_tasks was a success
        failed_nums = {t.ep_num for t in failed_tasks}
        for task in current_tasks:
            if task.ep_num not in failed_nums:
                completed_numbers.add(task.ep_num)

        if not failed_tasks:
            current_tasks = []
            break

        from InquirerPy import inquirer

        try:
            retry = inquirer.confirm(
                message=f"{len(failed_tasks)} episode(s) failed. Retry them?",
                default=True,
            ).execute()
        except KeyboardInterrupt:
            retry = False

        current_tasks = failed_tasks
        if not retry:
            break

    total_success = len(completed_numbers)
    failed_count = len(current_tasks)

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
            title="[bold cyan] Download Complete [/bold cyan]",
            border_style="cyan",
            expand=True,
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

    with _fun_status("search"):
        client._ensure_playwright()
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
        "\n  [cyan]shinkansen episodes <session>[/cyan]"
        "\n  [cyan]shinkansen download <session> --episode 1[/cyan]"
    )


@cli.command()
@click.argument("session")
@click.option("-p", "--page", default=0, type=int, help="Page number (0=all)")
def episodes(session, page):
    """List episodes for an anime (use session from search)."""
    client = get_client()

    if page > 0:
        with _fun_status("episodes"):
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
        with _fun_status("episodes"):
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

    with _fun_status("episodes"):
        ep_session = client.get_episode_session(session, episode_num)

    if not ep_session:
        console.print(f"[red]Episode {episode_num} not found.[/red]")
        return

    with _fun_status("sources"):
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
    with _fun_status("episodes"):
        eps = client.get_episodes(session)

    ep_session_map = {ep.number: ep.session for ep in eps}

    if episode:
        ep_numbers = [episode]
    elif ep_range:
        try:
            ep_numbers = parse_range(ep_range)
        except ValueError:
            console.print(
                "[red]✗ Invalid episodes format![/red] Use '-e 1-5' or '-e 1,3,5' or '-e all'"
            )
            return
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
    """Render a professional, full-width welcome banner."""
    from rich.panel import Panel
    from rich.align import Align
    from rich.text import Text
    from rich.console import Group

    logo = (
        "[bold cyan]"
        "  ╔═╗ ╦ ╦ ╦ ╔╗╤ ╦╔═ ╔═╗ ╔╗╤ ╔═╗ ╔═╗ ╔╗╤\n"
        "  ╚═╗ ╠═╣ ║ ║║║ ╠╩╗ ╠═╣ ║║║ ╚═╗ ║╣  ║║║\n"
        "  ╚═╝ ╩ ╩ ╩ ╝╚╝ ╩ ╩ ╩ ╩ ╝╚╝ ╚═╝ ╚═╝ ╝╚╝"
        "[/bold cyan]"
    )
    logo_text = Text.from_markup(logo)

    # Compact metadata line
    output_dir = get_config("output_dir", "downloads")
    meta = Text.from_markup(f"[dim]v1.0.6  ·  AnimePahe  ·  ~/{output_dir}[/dim]")

    # Recent activity as a compact one-liner
    history = load_history()
    recent_line = Text.from_markup("[dim]No recent downloads[/dim]")
    if history:
        recent_anime = []
        for h in reversed(history):
            name = h.get("anime")
            if name and name not in recent_anime:
                recent_anime.append(name)
            if len(recent_anime) >= 3:
                break
        if recent_anime:
            parts = "  ·  ".join(recent_anime)
            recent_line = Text.from_markup(
                f"[dim]Recent:[/dim]  [white]{parts}[/white]"
            )

    content = Group(
        Text(""),
        Align.center(logo_text),
        Text(""),
        Align.center(meta),
        Align.center(recent_line),
        Text(""),
    )

    return Panel(
        content,
        border_style="cyan",
        expand=True,
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
    with _fun_status("search"):
        client._ensure_playwright()
        results = client.search(query)

    if not results:
        console.print("\n[yellow]✗ No anime found matching that name.[/yellow]")
        import time

        time.sleep(2)
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
    with _fun_status("episodes"):
        eps = client.get_episodes(session)

    if not eps:
        console.print("[red]No episodes found.[/red]")
        return

    console.print(f"[bold]Found {len(eps)} episodes[/bold]")

    ep_session_map = {ep.number: ep.session for ep in eps}

    def validate_eps(result: str) -> bool:
        if result.lower() == "all":
            return True
        try:
            parse_range(result)
            return True
        except ValueError:
            return False

    # Step 4: Select episodes
    try:
        ep_choices = inquirer.text(
            message="Episodes to download (e.g., 1-5, 1,3,5 or 'all'):",
            default="all",
            validate=validate_eps,
            invalid_message="Invalid format! Use ranges (1-5) or commas (1,3,5)",
        ).execute()
    except KeyboardInterrupt:
        return

    if ep_choices.lower() == "all":
        ep_numbers = [ep.number for ep in eps]
    else:
        try:
            ep_numbers = parse_range(ep_choices)
        except ValueError:
            console.print(
                "[red]✗ Invalid episodes format![/red] Use numbers like '1-5', '1,3,5', or 'all'"
            )
            return

    # Validate that at least some requested episodes exist
    available_eps = set(ep_session_map.keys())
    valid_eps = [ep for ep in ep_numbers if ep in available_eps]

    if not valid_eps:
        min_ep = min(available_eps) if available_eps else 0
        max_ep = max(available_eps) if available_eps else 0
        console.print(
            f"\n[red]✗ None of requested episodes {ep_numbers} are available.[/red]"
        )
        console.print(
            f"[yellow]  This anime's available episodes are: {min_ep} to {max_ep}[/yellow]"
        )
        return

    if len(valid_eps) < len(ep_numbers):
        missing = sorted(list(set(ep_numbers) - set(valid_eps)))
        console.print(
            f"\n[yellow]⚠ Warning: Episodes {missing} are not available and will be skipped.[/yellow]"
        )
        ep_numbers = valid_eps

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
    console.print("\n[dim]Use 'shinkansen config set <key> <value>' to change[/dim]")


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
