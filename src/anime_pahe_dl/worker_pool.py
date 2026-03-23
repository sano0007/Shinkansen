"""Parallel Playwright worker pool for batch episode preparation and download.

Spawns N independent Playwright browser instances, each in its own thread
(own greenlet), to prepare episodes in true parallel. Downloads run in a
separate thread pool bounded by a configurable limit.

Architecture::

    Main Thread (Rich Live UI)
        │
        ├── PrepareWorker 0  (own thread + Playwright)
        ├── PrepareWorker 1  (own thread + Playwright)
        ├── PrepareWorker 2  (own thread + Playwright)
        │
        └── Download ThreadPoolExecutor (bounded)
"""

import logging
import queue
import re
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table

from anime_pahe_dl.client import AnimePaheClient, Source
from anime_pahe_dl.downloader import Downloader, PreparedDownload, safe_filename
from anime_pahe_dl.utils import select_source

logger = logging.getLogger(__name__)
console = Console()

# Sentinel value to signal workers to exit
_POISON = None


# ── Data structures ──────────────────────────────────────────────


@dataclass
class EpisodeTask:
    """Work item for the prepare worker queue."""

    ep_num: int
    ep_session: str
    anime_session: str
    anime_name: str
    quality: str
    prefer_dub: bool


@dataclass
class EpisodeResult:
    """Result from a prepare worker, consumed by the download coordinator."""

    ep_num: int
    source: Optional[Source] = None
    prepared: Optional[PreparedDownload] = None
    output_path: Optional[str] = None
    error: Optional[str] = None


# ── Progress tracker ─────────────────────────────────────────────


class ProgressTracker:
    """Thread-safe state for the Rich Live display."""

    def __init__(self, total: int, anime_name: str):
        self._lock = threading.Lock()
        self.total = total
        self.anime_name = anime_name
        self.worker_status: dict[int, str] = {}
        self.download_status: dict[int, str] = {}
        self._spinners: dict[int, Spinner] = {}
        self.completed = 0
        self.failed = 0
        self.skipped = 0
        self._done = threading.Event()

    def set_worker(self, worker_id: int, status: str):
        with self._lock:
            self.worker_status[worker_id] = status
            if worker_id not in self._spinners:
                self._spinners[worker_id] = Spinner("dots", text=status)
            else:
                self._spinners[worker_id].update(text=status)

    def clear_worker(self, worker_id: int):
        with self._lock:
            self.worker_status.pop(worker_id, None)
            self._spinners.pop(worker_id, None)

    def set_download(self, ep_num: int, status: str):
        with self._lock:
            self.download_status[ep_num] = status

    def clear_download(self, ep_num: int):
        with self._lock:
            self.download_status.pop(ep_num, None)

    def add_completed(self):
        with self._lock:
            self.completed += 1

    def add_failed(self):
        with self._lock:
            self.failed += 1

    def add_skipped(self):
        with self._lock:
            self.skipped += 1

    def mark_done(self):
        self._done.set()

    @property
    def is_done(self) -> bool:
        return self._done.is_set()

    def render(self) -> Panel:
        with self._lock:
            workers = self.worker_status.copy()
            downloads = self.download_status.copy()
            completed = self.completed
            failed = self.failed
            skipped = self.skipped
            # we need to shallow copy spinners to avoid mutation during iteration
            # but Spinner handles its own animation time so we just grab the ref
            spinners = dict(self._spinners)

        status_table = Table(show_header=False, box=None, padding=(0, 2))
        status_table.add_column("Type", style="dim", justify="right")
        status_table.add_column("Content")

        if workers:
            for wid in sorted(workers):
                spinner = spinners.get(wid, workers[wid])
                status_table.add_row(f"[cyan]W{wid}[/cyan]", spinner)
        else:
            status_table.add_row("[cyan]Workers[/cyan]", "[dim]Idle[/dim]")

        if downloads:
            for ep in sorted(downloads):
                # parse the percentage if available to make a pseudo bar
                text = downloads[ep]
                status_table.add_row(f"[magenta]Ep {ep}[/magenta]", text)
        else:
            status_table.add_row("[magenta]Downloads[/magenta]", "[dim]Idle[/dim]")

        done_total = completed + failed + skipped
        progress_text = (
            f"[green]✓ {completed} done[/green]    "
            f"[yellow]⊘ {skipped} skipped[/yellow]    "
            f"[red]✗ {failed} failed[/red]    "
            f"[dim]({done_total}/{self.total})[/dim]"
        )

        content = Group(status_table, Rule(style="dim"), progress_text)

        return Panel(
            content,
            title=f"[bold white]{self.anime_name}[/bold white]",
            border_style="cyan",
            expand=False,
        )


# ── Prepare worker ───────────────────────────────────────────────


class PrepareWorker(threading.Thread):
    """One Playwright browser instance running in its own thread.

    Each worker creates its own AnimePaheClient + Downloader inside run(),
    so sync_playwright().start() is called in this thread and the greenlet
    is bound here — satisfying the Playwright sync API constraint.
    """

    def __init__(
        self,
        worker_id: int,
        task_queue: queue.Queue,
        result_queue: queue.Queue,
        cookies_ready: threading.Event,
        output_dir: str,
        tracker: ProgressTracker,
    ):
        super().__init__(daemon=True, name=f"PrepareWorker-{worker_id}")
        self._id = worker_id
        self._task_queue = task_queue
        self._result_queue = result_queue
        self._cookies_ready = cookies_ready
        self._output_dir = output_dir
        self._tracker = tracker
        self._client: Optional[AnimePaheClient] = None
        self._downloader: Optional[Downloader] = None
        self._stop_event = threading.Event()

    def run(self):
        try:
            self._init_playwright()
            self._process_loop()
        except Exception as e:
            logger.error(f"Worker {self._id} crashed: {e}")
        finally:
            self._cleanup()
            self._tracker.clear_worker(self._id)

    def _init_playwright(self):
        """Initialize Playwright in this thread (binds greenlet here)."""
        self._tracker.set_worker(self._id, "initializing...")

        self._client = AnimePaheClient()
        self._downloader = Downloader(self._output_dir)

        if self._id == 0:
            # Worker 0 clears Cloudflare first, saves cookies for others
            self._tracker.set_worker(self._id, "clearing Cloudflare...")
            self._client._ensure_playwright()
            self._cookies_ready.set()
        else:
            # Wait for worker 0 to save cookies
            self._cookies_ready.wait(timeout=120)
            self._client._load_cached_cookies()
            self._client._ensure_playwright()

        # Share Playwright context between client and downloader
        if self._client._pw_context:
            self._downloader.set_playwright_context(self._client._pw_context)

    def _process_loop(self):
        """Pull tasks from the queue and prepare episodes."""
        while not self._stop_event.is_set():
            try:
                task = self._task_queue.get(timeout=1)
            except queue.Empty:
                continue

            if task is _POISON:
                self._task_queue.task_done()
                break

            try:
                result = self._prepare_one(task)
                self._result_queue.put(result)
            except Exception as e:
                logger.warning(f"Worker {self._id} failed on ep {task.ep_num}: {e}")
                self._result_queue.put(EpisodeResult(ep_num=task.ep_num, error=str(e)))
            finally:
                self._task_queue.task_done()

    def _prepare_one(self, task: EpisodeTask) -> EpisodeResult:
        """Fetch sources + resolve download URL for one episode."""
        self._tracker.set_worker(self._id, f"Ep {task.ep_num} sources...")

        srcs = self._client.get_sources(task.anime_session, task.ep_session)
        # Re-share context after get_sources (it may initialize Playwright)
        if self._client._pw_context:
            self._downloader.set_playwright_context(self._client._pw_context)

        source = select_source(srcs, task.quality, task.prefer_dub)
        if not source:
            return EpisodeResult(ep_num=task.ep_num, error="No matching source")

        self._tracker.set_worker(self._id, f"Ep {task.ep_num} resolving...")
        prepared = self._downloader.prepare(source.url)
        if not prepared:
            return EpisodeResult(ep_num=task.ep_num, error="Failed to resolve URL")

        self._tracker.set_worker(self._id, f"Ep {task.ep_num} ready")
        return EpisodeResult(ep_num=task.ep_num, source=source, prepared=prepared)

    def stop(self):
        self._stop_event.set()

    def _cleanup(self):
        """Close Playwright browser + context."""
        try:
            if self._downloader:
                self._downloader.close()
            if self._client:
                self._client.close()
        except Exception:
            pass


# ── Worker pool ──────────────────────────────────────────────────


class WorkerPool:
    """Orchestrates parallel episode preparation and downloading.

    Args:
        num_workers: Number of parallel Playwright instances.
        max_downloads: Max concurrent file downloads.
        output_dir: Base output directory for downloads.
        on_complete: Optional callback(ep_num, quality, file_path) after each download.
    """

    def __init__(
        self,
        num_workers: int = 3,
        max_downloads: int = 5,
        output_dir: str = "downloads",
        on_complete: Optional[Callable] = None,
    ):
        self._num_workers = max(1, num_workers)
        self._max_downloads = max(1, max_downloads)
        self._output_dir = output_dir
        self._on_complete = on_complete

        self._task_queue: queue.Queue = queue.Queue()
        self._result_queue: queue.Queue = queue.Queue()
        self._cookies_ready = threading.Event()

        self._workers: list[PrepareWorker] = []
        self._tracker: Optional[ProgressTracker] = None
        self._original_sigint = None

    def run(self, tasks: list[EpisodeTask]) -> tuple[int, int]:
        """Run the full prepare+download pipeline.

        Returns (success_count, failed_count).
        """
        if not tasks:
            return 0, 0

        anime_name = tasks[0].anime_name
        quality = tasks[0].quality

        # Filter out already-downloaded episodes
        active_tasks = []
        skipped = 0
        for task in tasks:
            if self._file_exists(task):
                console.print(
                    f"  [yellow]Already exists, skipping: "
                    f"{safe_filename(task.anime_name, task.ep_num, task.quality)}[/yellow]"
                )
                skipped += 1
            else:
                active_tasks.append(task)

        total = len(tasks)
        self._tracker = ProgressTracker(total, anime_name)
        self._tracker.skipped = skipped

        if not active_tasks:
            console.print("[green]All episodes already downloaded![/green]")
            return skipped, 0

        # Fill the task queue
        for task in active_tasks:
            self._task_queue.put(task)

        # Add poison pills (one per worker)
        for _ in range(self._num_workers):
            self._task_queue.put(_POISON)

        # Create workers
        self._workers = [
            PrepareWorker(
                worker_id=i,
                task_queue=self._task_queue,
                result_queue=self._result_queue,
                cookies_ready=self._cookies_ready,
                output_dir=self._output_dir,
                tracker=self._tracker,
            )
            for i in range(self._num_workers)
        ]

        # Install Ctrl+C handler (only works from main thread)
        try:
            self._original_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._handle_sigint)
        except (ValueError, OSError):
            pass  # Not on main thread (e.g., in tests)

        try:
            # Start workers
            for w in self._workers:
                w.start()

            # Run download coordinator + progress display concurrently
            coordinator = threading.Thread(
                target=self._download_coordinator,
                args=(len(active_tasks), anime_name, quality),
                daemon=True,
                name="DownloadCoordinator",
            )
            coordinator.start()

            # Main thread: Rich Live progress display
            self._show_progress()

            # Wait for coordinator to finish
            coordinator.join(timeout=5)

        finally:
            self._shutdown()
            if self._original_sigint:
                try:
                    signal.signal(signal.SIGINT, self._original_sigint)
                except (ValueError, OSError):
                    pass

        return self._tracker.completed + self._tracker.skipped, self._tracker.failed

    def _download_coordinator(self, num_active: int, anime_name: str, quality: str):
        """Consume prepared results and dispatch downloads."""
        remaining = num_active

        with ThreadPoolExecutor(
            max_workers=self._max_downloads,
            thread_name_prefix="Download",
        ) as pool:
            futures = {}

            while remaining > 0:
                try:
                    result = self._result_queue.get(timeout=2)
                except queue.Empty:
                    # Check if all workers are dead
                    if all(not w.is_alive() for w in self._workers):
                        break
                    continue

                if result.error:
                    logger.warning(f"Episode {result.ep_num}: {result.error}")
                    self._tracker.add_failed()
                    remaining -= 1
                    continue

                # Start download
                self._tracker.set_download(result.ep_num, "starting...")
                future = pool.submit(self._do_download, result, anime_name, quality)
                futures[future] = result

                # Check for completed downloads
                done_futures = [f for f in futures if f.done()]
                for f in done_futures:
                    res = futures.pop(f)
                    try:
                        path = f.result()
                        if path:
                            self._tracker.add_completed()
                            self._tracker.clear_download(res.ep_num)
                            if self._on_complete:
                                self._on_complete(res.ep_num, res.source.quality, path)
                        else:
                            self._tracker.add_failed()
                            self._tracker.clear_download(res.ep_num)
                    except Exception as e:
                        logger.error(f"Download ep {res.ep_num} error: {e}")
                        self._tracker.add_failed()
                        self._tracker.clear_download(res.ep_num)
                    remaining -= 1

            # Wait for remaining downloads
            for future in as_completed(futures):
                res = futures[future]
                try:
                    path = future.result()
                    if path:
                        self._tracker.add_completed()
                        if self._on_complete:
                            self._on_complete(res.ep_num, res.source.quality, path)
                    else:
                        self._tracker.add_failed()
                except Exception:
                    self._tracker.add_failed()
                self._tracker.clear_download(res.ep_num)
                remaining -= 1

        self._tracker.mark_done()

    def _do_download(
        self, result: EpisodeResult, anime_name: str, quality: str
    ) -> Optional[str]:
        """Download a single prepared episode (runs in download thread pool)."""
        ep = result.ep_num

        # Get expected file size for progress tracking
        total_size = 0
        try:
            session = requests.Session()
            head = session.head(
                result.prepared.video_url,
                headers=result.prepared.headers,
                timeout=10,
                allow_redirects=True,
            )
            total_size = int(head.headers.get("content-length", 0))
        except Exception:
            pass

        size_label = ""
        if total_size > 0:
            size_label = f" ({total_size / 1024 / 1024:.0f}MB)"

        self._tracker.set_download(ep, f"downloading...{size_label}")

        # Start download in a background thread so we can poll progress
        dl = Downloader(self._output_dir)
        q = result.source.quality.replace("p", "")

        # Determine the output path for progress polling
        from anime_pahe_dl.config import get_config as _cfg

        create_folder = _cfg("create_folder", True)
        fname = safe_filename(anime_name, ep, q)
        base = Path(self._output_dir)
        if create_folder:
            slug = re.sub(r"[^0-9A-Za-z]+", "_", anime_name).strip("_")
            slug = re.sub(r"_+", "_", slug) or "Anime"
            out_path = base / slug / fname
        else:
            out_path = base / fname

        # Run the actual download in a sub-thread so we can poll file size
        dl_result = [None]
        dl_done = threading.Event()

        def _run():
            dl_result[0] = dl.download_prepared(
                result.prepared, anime_name, ep, q, quiet=True
            )
            dl_done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        # Poll file size for progress updates
        while not dl_done.is_set():
            try:
                current = out_path.stat().st_size if out_path.exists() else 0
            except OSError:
                current = 0
            if total_size > 0:
                pct = min(100, int(current / total_size * 100))
                mb_done = current / 1024 / 1024
                mb_total = total_size / 1024 / 1024
                self._tracker.set_download(
                    ep, f"{pct}% ({mb_done:.0f}/{mb_total:.0f}MB)"
                )
            dl_done.wait(timeout=1)

        t.join(timeout=5)
        path = dl_result[0]

        if path:
            self._tracker.set_download(ep, "[green]done[/green]")
        else:
            self._tracker.set_download(ep, "[red]failed[/red]")

        return path

    def _show_progress(self):
        """Rich Live progress display on the main thread."""
        with Live(
            self._tracker.render(),
            console=console,
            refresh_per_second=2,
            transient=False,
        ) as live:
            while not self._tracker.is_done:
                live.update(self._tracker.render())
                time.sleep(0.5)
            # Final render
            live.update(self._tracker.render())

    def _file_exists(self, task: EpisodeTask) -> bool:
        """Check if episode file already exists on disk."""
        expected = safe_filename(task.anime_name, task.ep_num, task.quality)
        base = Path(self._output_dir)

        # Check with and without anime subfolder
        slug = re.sub(r"[^0-9A-Za-z]+", "_", task.anime_name).strip("_")
        slug = re.sub(r"_+", "_", slug) or "Anime"

        return (base / expected).exists() or (base / slug / expected).exists()

    def _handle_sigint(self, signum, frame):
        """Graceful Ctrl+C: stop workers and let downloads finish."""
        console.print("\n[yellow]Stopping... (waiting for current tasks)[/yellow]")
        self._shutdown()
        if self._original_sigint:
            signal.signal(signal.SIGINT, self._original_sigint)

    def _shutdown(self):
        """Stop all workers and clean up."""
        # Signal workers to stop
        for w in self._workers:
            w.stop()

        # Drain the task queue so workers aren't blocked on put
        while not self._task_queue.empty():
            try:
                self._task_queue.get_nowait()
                self._task_queue.task_done()
            except queue.Empty:
                break

        # Wait for workers to finish
        for w in self._workers:
            w.join(timeout=10)
