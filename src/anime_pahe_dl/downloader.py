"""
Download engine for anime-pahe-dl.

The download flow (from your screenshots):

    pahe.win/xxxxx
        ↓ (navigate, wait for timer, extract kwik link)
    kwik.cx/f/xxxxxxxxx
        ↓ (find hidden form, extract CSRF token)
    POST kwik.cx/d/xxxxxxxxx with _token
        ↓ (302 redirect)
    Direct .mp4 URL → download with progress bar

Key learnings from autopahe:
- pahe.win has a countdown timer + "Continue" link (a.redirect)
- kwik.cx/f/ has a hidden <form> with a CSRF _token
- POST to /d/ (not /f/) with the token → 302 to direct video URL
- Must transfer cookies from Playwright to requests.Session
- Must match User-Agent between Playwright and requests
- Referer must be the kwik /f/ URL, Origin must be kwik domain
"""

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
import tqdm
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


@dataclass
class PreparedDownload:
    """Pre-resolved download info ready for file download.

    Contains the direct video URL and headers extracted via Playwright,
    so the actual download only needs requests (no browser).
    """
    video_url: str
    headers: dict
    kwik_url: str  # For logging/debugging


def _setup_session() -> requests.Session:
    """Create a robust HTTP session with retries."""
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def safe_filename(anime_name: str, episode: int, quality: str = "720p") -> str:
    """Build a clean, safe filename for the downloaded video."""
    slug = re.sub(r"[^0-9A-Za-z]+", "_", anime_name).strip("_")
    slug = re.sub(r"_+", "_", slug) or "Anime"
    ep_str = str(episode).zfill(2)
    if not quality.endswith("p"):
        quality = f"{quality}p"
    return f"{slug}_Ep{ep_str}_{quality}.mp4"


class Downloader:
    """
    Handles the full download pipeline:
    pahe.win → kwik.cx → direct MP4 download

    Uses Playwright for page navigation (pahe.win timer, kwik form)
    and requests for the actual file download (faster, resumable).

    IMPORTANT: Must share the same Playwright context as the client
    to avoid sync/async event loop conflicts. Pass the client's
    context via set_playwright_context() or the constructor.
    """

    def __init__(self, output_dir: str = "downloads", pw_context=None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._pw_context = pw_context  # Shared from AnimePaheClient
        self._owns_pw = False  # Whether we created the context ourselves

    def set_playwright_context(self, context):
        """Set a shared Playwright browser context (from the client).

        This avoids the "Sync API inside asyncio loop" error that occurs
        when multiple sync_playwright().start() calls happen in the same
        process. The client already has a working context with Cloudflare
        cookies — we just reuse it.
        """
        self._pw_context = context

    def _ensure_playwright(self):
        """Ensure we have a Playwright context available.

        Prefers the shared context from the client. Only creates its own
        as a last resort (standalone usage without the client).
        """
        if self._pw_context is not None:
            return

        # Last resort: create our own (only works if no other Playwright is running)
        logger.warning(
            "No shared Playwright context — creating standalone. "
            "Prefer passing the client's context via set_playwright_context()."
        )
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        browser = self._pw.chromium.launch(headless=True)
        self._pw_context = browser.new_context()
        self._owns_pw = True

    # ── Step 1: Resolve pahe.win → kwik.cx/f/ URL ────────────────

    def _resolve_pahewin(self, pahewin_url: str) -> Optional[str]:
        """
        Navigate pahe.win, wait for the timer, extract kwik.cx redirect.

        The pahe.win page shows a countdown, then reveals an <a class="redirect">
        link pointing to kwik.cx/f/xxxxx.
        """
        self._ensure_playwright()
        page = self._pw_context.new_page()
        try:
            logger.info(f"Resolving pahe.win: {pahewin_url}")
            page.goto(pahewin_url, wait_until="domcontentloaded", timeout=60_000)

            # Method 1: Wait for the redirect anchor to have a kwik URL
            try:
                page.wait_for_function(
                    """() => {
                        const a = document.querySelector('a.redirect');
                        return a && (a.href.includes('kwik.cx') || a.href.includes('kwik.si'));
                    }""",
                    timeout=15_000,
                )
                kwik_url = page.eval_on_selector("a.redirect", "el => el.href")
                if kwik_url:
                    logger.info(f"Got kwik URL from redirect anchor: {kwik_url}")
                    return kwik_url
            except Exception as e:
                logger.debug(f"Redirect anchor method failed: {e}")

            # Method 2: Extract from page HTML via regex
            try:
                html = page.content()
                match = re.search(r'kwik\.(cx|si)/f/([a-zA-Z0-9]+)', html)
                if match:
                    kwik_url = f"https://kwik.{match.group(1)}/f/{match.group(2)}"
                    logger.info(f"Got kwik URL from HTML regex: {kwik_url}")
                    return kwik_url
            except Exception as e:
                logger.debug(f"Regex extraction failed: {e}")

            logger.error("Could not extract kwik URL from pahe.win")
            return None

        except Exception as e:
            logger.error(f"pahe.win navigation failed: {e}")
            return None
        finally:
            page.close()

    # ── Step 2: Extract CSRF token + cookies from kwik.cx/f/ ─────

    def _extract_kwik_token(self, kwik_url: str) -> Optional[dict]:
        """
        Navigate kwik.cx/f/xxxxx, find the hidden form, extract:
        - CSRF _token
        - Cookies (needed for the POST)
        - User-Agent (must match between browser and requests)

        Returns dict with {token, cookies, user_agent, url} or None.
        """
        self._ensure_playwright()
        page = self._pw_context.new_page()
        try:
            logger.info(f"Extracting token from: {kwik_url}")
            page.goto(kwik_url, wait_until="domcontentloaded", timeout=60_000)

            # Wait for the form to appear
            try:
                page.wait_for_selector("form", timeout=10_000)
            except Exception:
                pass
            time.sleep(3)  # Extra wait for any JS to finish

            # Find the form and hidden input
            form = page.query_selector("form")
            if not form:
                # Debug: check what's on the page
                title = page.title()
                logger.error(f"No form found on kwik page. Title: {title}")
                content = page.content().lower()
                if "cloudflare" in content:
                    logger.error("Cloudflare protection detected on kwik")
                if "captcha" in content:
                    logger.error("CAPTCHA detected on kwik")
                return None

            hidden = form.query_selector('input[type="hidden"]')
            if not hidden:
                logger.error("No hidden input (CSRF token) found in form")
                return None

            token = hidden.get_attribute("value")
            if not token:
                logger.error("CSRF token is empty")
                return None

            # Get the browser's User-Agent
            ua = page.evaluate("navigator.userAgent") or ""

            # Get cookies from browser context
            try:
                cookies = self._pw_context.cookies([kwik_url])
            except Exception:
                cookies = self._pw_context.cookies()

            return {
                "token": token,
                "cookies": cookies,
                "user_agent": ua,
                "url": kwik_url,
            }

        except Exception as e:
            logger.error(f"Token extraction failed: {e}")
            return None
        finally:
            page.close()

    # ── Step 3: POST to kwik /d/ and get redirect to .mp4 ────────

    def _get_video_url(self, kwik_info: dict) -> Optional[tuple[str, dict]]:
        """
        POST the CSRF token to kwik.cx/d/ to get the direct video URL.

        kwik.cx responds with a 302 redirect to the actual .mp4 URL.
        We need to NOT follow the redirect (allow_redirects=False) and
        grab the Location header.

        Returns (video_url, headers_for_download) or None.
        """
        kwik_url = kwik_info["url"]
        token = kwik_info["token"]
        ua = kwik_info["user_agent"]
        cookies = kwik_info["cookies"]

        # Build POST URL: /f/ → /d/
        post_url = kwik_url.replace("/f/", "/d/")
        parsed = urlparse(kwik_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # Build headers that match the browser
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": kwik_url,
            "Origin": origin,
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Content-Type": "application/x-www-form-urlencoded",
            # Chromium-specific headers
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }

        # Build session and transfer cookies
        session = _setup_session()
        for c in cookies:
            domain = c.get("domain") or parsed.netloc
            session.cookies.set(c["name"], c["value"], domain=domain)

        # POST with the token
        logger.info(f"POSTing to {post_url}")
        try:
            resp = session.post(
                post_url,
                data={"_token": token},
                headers=headers,
                allow_redirects=False,
                timeout=30,
            )

            if resp.status_code in (301, 302, 303, 307, 308):
                video_url = resp.headers.get("Location")
                if video_url:
                    logger.info(f"Got video URL: {video_url[:80]}...")
                    # Return download headers (referer is important)
                    dl_headers = {
                        "User-Agent": ua,
                        "Referer": kwik_url,
                    }
                    return video_url, dl_headers
                else:
                    logger.error("Redirect without Location header")

            elif resp.status_code == 200:
                # Try to find URL in response body
                match = re.search(
                    r'https?://[^\s"\'<>]+\.m(?:p4|3u8)[^\s"\'<>]*',
                    resp.text,
                )
                if match:
                    return match.group(0), {"User-Agent": ua, "Referer": kwik_url}

            elif resp.status_code == 403:
                logger.error(
                    "403 Forbidden — token may be expired or cookies invalid. "
                    "Try again (tokens are time-limited)."
                )
            else:
                logger.error(f"Unexpected status: {resp.status_code}")

        except Exception as e:
            logger.error(f"POST failed: {e}")

        return None

    # ── Step 4: Download the actual video file ───────────────────

    def _download_file(
        self,
        video_url: str,
        headers: dict,
        filename: str,
        episode_label: str = "",
        output_dir: Optional[Path] = None,
    ) -> Optional[str]:
        """
        Download a video file with progress bar and resume support.

        Uses requests (not Playwright) for speed and resume capability.
        """
        if output_dir is None:
            output_dir = self.output_dir
        output_path = output_dir / filename

        # Handle existing partial files (resume)
        existing_size = output_path.stat().st_size if output_path.exists() else 0
        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"
            logger.info(f"Resuming from {existing_size} bytes")

        session = _setup_session()

        for attempt in range(1, 4):
            try:
                resp = session.get(
                    video_url, headers=headers, stream=True, timeout=30
                )

                if resp.status_code == 416:
                    # Range not satisfiable — file is already complete
                    logger.info("File already complete")
                    return str(output_path)

                if resp.status_code not in (200, 206):
                    logger.error(f"Download HTTP {resp.status_code}")
                    return None

                total = int(resp.headers.get("content-length", 0)) + existing_size
                mode = "ab" if existing_size else "wb"

                with open(output_path, mode) as f, tqdm.tqdm(
                    total=total,
                    initial=existing_size,
                    unit="B",
                    unit_scale=True,
                    desc=episode_label or filename,
                    ncols=80,
                    unit_divisor=1024,
                ) as bar:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
                            bar.update(len(chunk))

                return str(output_path)

            except Exception as e:
                logger.warning(f"Download attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    time.sleep(3)

        return None

    # ── Public API: full download pipeline ───────────────────────

    def prepare(self, pahewin_url: str) -> Optional[PreparedDownload]:
        """Resolve pahe.win → kwik.cx → direct video URL (Playwright-heavy).

        This does all the browser automation work upfront:
        1. Resolve pahe.win redirect to kwik.cx/f/ URL
        2. Extract CSRF token from kwik page
        3. POST to get direct .mp4 video URL

        Returns a PreparedDownload that can be passed to download_prepared()
        which only needs requests (no Playwright), making it safe to run
        in a background thread while Playwright prepares the next episode.
        """
        # Step 1: Resolve pahe.win redirect
        if "pahe.win" in pahewin_url:
            kwik_url = self._resolve_pahewin(pahewin_url)
        elif "kwik.cx" in pahewin_url or "kwik.si" in pahewin_url:
            kwik_url = pahewin_url
        else:
            logger.error(f"Unknown URL format: {pahewin_url}")
            return None

        if not kwik_url:
            return None

        # Step 2: Extract CSRF token from kwik page
        kwik_info = self._extract_kwik_token(kwik_url)
        if not kwik_info:
            return None

        # Step 3: POST to get direct video URL
        result = self._get_video_url(kwik_info)
        if not result:
            return None

        video_url, dl_headers = result
        return PreparedDownload(
            video_url=video_url,
            headers=dl_headers,
            kwik_url=kwik_url,
        )

    def prepare_batch(
            self,
            episodes: list[tuple[int, str]],
            max_workers: int = 3,
    ) -> dict[int, Optional[PreparedDownload]]:
        """Resolve multiple pahe.win URLs in parallel using concurrent browser tabs.

        Runs up to max_workers Playwright page chains simultaneously.
        Each chain is independent: pahe.win → kwik.cx → direct .mp4 URL.

        Args:
            episodes: List of (episode_number, pahewin_url) tuples.
            max_workers: Max concurrent Playwright page chains.

        Returns:
            {episode_number: PreparedDownload or None}
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Ensure Playwright is initialized on the calling thread before spawning workers
        self._ensure_playwright()

        results: dict[int, Optional[PreparedDownload]] = {}

        def _prep_one(ep_num: int, url: str) -> tuple[int, Optional[PreparedDownload]]:
            return ep_num, self.prepare(url)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_prep_one, ep_num, url): ep_num
                for ep_num, url in episodes
            }
            for future in as_completed(futures):
                ep_num, prepared = future.result()
                results[ep_num] = prepared

        return results

    def download_prepared(
            self,
            prepared: PreparedDownload,
            anime_name: str = "Anime",
            episode: int = 1,
            quality: str = "720p",
    ) -> Optional[str]:
        """Download a pre-prepared video file (no Playwright needed).

        This only uses requests for the actual file download, so it can
        safely run in a background thread while Playwright prepares the
        next episode on the main thread.
        """
        filename = safe_filename(anime_name, episode, quality)

        from anime_pahe_dl.config import get_config
        create_folder = get_config("create_folder", True)

        if create_folder:
            anime_folder = re.sub(r"[^0-9A-Za-z]+", "_", anime_name).strip("_")
            anime_folder = re.sub(r"_+", "_", anime_folder) or "Anime"
            output_dir = self.output_dir / anime_folder
            output_dir.mkdir(parents=True, exist_ok=True)
        else:
            output_dir = self.output_dir

        return self._download_file(
            prepared.video_url, prepared.headers, filename,
            f"Ep {episode}", output_dir,
        )

    def download(
            self,
            pahewin_url: str,
            anime_name: str = "Anime",
            episode: int = 1,
            quality: str = "720p",
    ) -> Optional[str]:
        """Full download pipeline: pahe.win → kwik.cx → .mp4

        Convenience method that calls prepare() + download_prepared().
        """
        prepared = self.prepare(pahewin_url)
        if not prepared:
            return None
        return self.download_prepared(prepared, anime_name, episode, quality)

    def close(self):
        """Clean up Playwright resources (only if we created them)."""
        if not self._owns_pw:
            return  # Shared context — client will clean up
        if self._pw_context:
            try:
                self._pw_context.close()
            except Exception:
                pass
        if hasattr(self, '_pw') and self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass