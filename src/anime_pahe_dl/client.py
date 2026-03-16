"""
AnimePahe API client with Cloudflare bypass.

Strategy (learned from autopahe):
1. Try direct HTTP first (fast, no browser needed)
2. Fall back to Playwright only when Cloudflare blocks us
3. Cache aggressively to avoid repeat fetches

Key differences from your original:
- Uses animepahe.com/api JSON endpoints directly (not HTML scraping)
- No global browser state — context managed per-session
- Proper pagination support for large episode lists (e.g., Bleach 366 eps)
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# AnimePahe mirrors — tried in order
MIRRORS = [
    "https://animepahe.si",
    "https://animepahe.com",
    # "https://animepahe.ru",
    "https://animepahe.org",
]

# Persistent cookie cache — avoids Cloudflare challenge on repeat runs
COOKIE_CACHE_DIR = Path.home() / ".anime-dl"
COOKIE_CACHE_FILE = COOKIE_CACHE_DIR / "cookies.json"
COOKIE_MAX_AGE = 25 * 60  # 25 minutes — CF cookies typically last ~30 min

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


@dataclass
class Anime:
    id: int
    session: str
    title: str
    episodes: int
    status: str
    year: str = ""
    poster: str = ""

    @classmethod
    def from_api(cls, data: dict) -> "Anime":
        return cls(
            id=data.get("id", 0),
            session=data.get("session", ""),
            title=data.get("title", "Unknown"),
            episodes=data.get("episodes", 0),
            status=data.get("status", "Unknown"),
            year=str(data.get("year", "")),
            poster=data.get("poster", ""),
        )


@dataclass
class Episode:
    number: int
    session: str
    title: str = ""
    snapshot: str = ""
    filler: bool = False


@dataclass
class Source:
    """A download source for an episode."""
    url: str  # pahe.win URL (from the dropdown)
    quality: str  # e.g. "1080p"
    audio: str  # e.g. "jpn" or "eng"
    fansub: str = ""
    size: str = ""

    @property
    def is_dub(self) -> bool:
        return self.audio.lower() in ("eng", "english")


def _build_session() -> requests.Session:
    """Build a requests session with retry logic and connection pooling."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session


class AnimePaheClient:
    """
    Client for the AnimePahe API.

    Architecture:
    - HTTP-first: tries direct API calls which are fast
    - Playwright fallback: only when Cloudflare blocks the HTTP request
    - Session reuse: single requests.Session for all HTTP calls

    Cloudflare session handling:
    - On first Playwright use, we visit the main animepahe page and wait
      for the DDoS challenge to clear. This establishes cookies in the
      browser context that all subsequent page navigations inherit.
    - We also transfer those cookies to the requests.Session so HTTP
      calls can work after Playwright has cleared Cloudflare.
    """

    def __init__(self):
        self._http = _build_session()
        self._pw = None           # Playwright instance (lazy)
        self._pw_context = None   # Browser context (lazy)
        self._base_url: Optional[str] = None  # Working mirror
        self._cf_cleared = False  # Whether Cloudflare has been cleared

        # Try loading cached cookies — may skip Playwright entirely
        self._load_cached_cookies()

    # ── Cookie cache ─────────────────────────────────────────────

    def _load_cached_cookies(self):
        """Load Cloudflare cookies from disk cache into the HTTP session.

        If valid cached cookies exist (not expired), we load them so that
        the first _api_get() call can succeed without Playwright.
        """
        if not COOKIE_CACHE_FILE.exists():
            return

        try:
            with open(COOKIE_CACHE_FILE) as f:
                cache = json.load(f)

            saved_at = cache.get("saved_at", 0)
            age = time.time() - saved_at
            if age > COOKIE_MAX_AGE:
                logger.debug(f"Cookie cache expired ({age:.0f}s old, max {COOKIE_MAX_AGE}s)")
                return

            cookies = cache.get("cookies", [])
            base_url = cache.get("base_url")

            if not cookies:
                return

            for c in cookies:
                self._http.cookies.set(
                    c["name"], c["value"],
                    domain=c.get("domain", ""),
                    path=c.get("path", "/"),
                )

            if base_url:
                self._base_url = base_url

            logger.info(f"Loaded {len(cookies)} cached cookies ({age:.0f}s old)")
        except (json.JSONDecodeError, IOError, KeyError) as e:
            logger.debug(f"Failed to load cookie cache: {e}")

    def _save_cookies_to_cache(self):
        """Save current Cloudflare cookies to disk for future runs.

        Called after a successful Cloudflare challenge clear. Saves both
        the Playwright browser cookies and the working mirror URL.
        """
        if not self._pw_context:
            return

        try:
            cookies = self._pw_context.cookies()
            if not cookies:
                return

            COOKIE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

            cache = {
                "saved_at": time.time(),
                "base_url": self._base_url,
                "cookies": [
                    {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ""),
                        "path": c.get("path", "/"),
                    }
                    for c in cookies
                ],
            }

            with open(COOKIE_CACHE_FILE, "w") as f:
                json.dump(cache, f)

            logger.info(f"Saved {len(cookies)} cookies to cache")
        except Exception as e:
            logger.debug(f"Failed to save cookie cache: {e}")

    # ── HTTP helpers ──────────────────────────────────────────────

    def _api_get(self, params: dict) -> Optional[dict]:
        """Try direct HTTP GET against all mirrors."""
        for base in MIRRORS:
            try:
                resp = self._http.get(
                    f"{base}/api", params=params, timeout=15
                )
                if resp.status_code == 200 and resp.content:
                    # Verify it's actually JSON, not a Cloudflare challenge page
                    try:
                        data = resp.json()
                        self._base_url = base
                        return data
                    except Exception:
                        logger.debug(f"Mirror {base} returned non-JSON (likely CF challenge)")
                        continue
                elif resp.status_code == 404:
                    # 404 from API usually means Cloudflare hasn't been cleared
                    logger.debug(f"Mirror {base} returned 404 (no CF session?)")
                    continue
            except Exception as e:
                logger.debug(f"Mirror {base} failed: {e}")
                continue
        return None

    def _api_get_with_fallback(self, params: dict, wait: int = 5) -> Optional[dict]:
        """Try HTTP first, fall back to Playwright if blocked."""
        result = self._api_get(params)
        if result:
            return result

        logger.info("HTTP failed, falling back to Playwright...")

        # Ensure Cloudflare is cleared before making API calls
        self._ensure_playwright()

        base = self._base_url or MIRRORS[0]
        url = f"{base}/api?" + "&".join(f"{k}={v}" for k, v in params.items())
        result = self._playwright_json(url, wait)

        # After Playwright succeeds, transfer cookies to HTTP session
        # so subsequent HTTP calls may work without Playwright
        if result:
            self._transfer_cookies_to_http()
            self._save_cookies_to_cache()

        return result

    def _transfer_cookies_to_http(self):
        """Transfer cookies from Playwright context to requests.Session.

        After Playwright clears Cloudflare, the browser context has valid
        cookies. Transferring them to the HTTP session lets subsequent
        direct HTTP calls work without needing the browser.
        """
        if not self._pw_context:
            return
        try:
            cookies = self._pw_context.cookies()
            for c in cookies:
                self._http.cookies.set(
                    c["name"], c["value"],
                    domain=c.get("domain", ""),
                    path=c.get("path", "/"),
                )
            logger.debug(f"Transferred {len(cookies)} cookies to HTTP session")
        except Exception as e:
            logger.debug(f"Cookie transfer failed: {e}")

    # ── Playwright helpers (lazy init) ────────────────────────────

    def _ensure_playwright(self):
        """Lazy-init Playwright browser context and clear Cloudflare.

        On first call:
        1. Launch headless Chromium
        2. Navigate to the main animepahe page
        3. Wait for Cloudflare DDoS challenge to resolve
        4. Transfer cookies to HTTP session for faster subsequent calls

        This is critical — without visiting the main page first,
        API endpoints return 404 because Cloudflare hasn't issued
        the session cookies yet.
        """
        if self._pw_context is not None:
            return

        from playwright.sync_api import sync_playwright

        logger.info("Starting Playwright browser...")
        self._pw = sync_playwright().start()
        browser = self._pw.chromium.launch(headless=True)
        self._pw_context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
        )

        # Visit main page to clear Cloudflare DDoS protection
        if not self._cf_cleared:
            self._clear_cloudflare()

    def _clear_cloudflare(self):
        """Visit the main AnimePahe page and wait for Cloudflare to clear.

        Cloudflare shows a "Checking your browser" interstitial that sets
        cookies after ~5 seconds. We wait until the page title no longer
        contains DDoS-related text.
        """
        base = self._base_url or MIRRORS[0]
        logger.info(f"Clearing Cloudflare on {base}...")
        page = self._pw_context.new_page()
        try:
            page.goto(base, wait_until="domcontentloaded", timeout=60_000)

            # Wait up to 30 seconds for the DDoS challenge to resolve
            for i in range(30):
                try:
                    title = page.title().lower()
                    if "ddos" not in title and "checking" not in title and "just a moment" not in title:
                        logger.info(f"Cloudflare cleared after {i+1}s (title: '{page.title()}')")
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)
            else:
                logger.warning("Cloudflare challenge may not have cleared (timeout)")

            # Extra wait for cookies to settle
            page.wait_for_timeout(2000)

            self._cf_cleared = True
            self._base_url = base

            # Transfer cookies to HTTP session
            self._transfer_cookies_to_http()

            # Persist cookies to disk for future runs
            self._save_cookies_to_cache()

        except Exception as e:
            logger.error(f"Cloudflare clearing failed: {e}")
        finally:
            page.close()

    def _playwright_json(self, url: str, wait: int = 5) -> Optional[dict]:
        """Fetch a URL with Playwright and parse JSON from body."""
        self._ensure_playwright()
        page = self._pw_context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(wait * 1000)
            body = page.evaluate("document.body.innerText")
            return json.loads(body)
        except Exception as e:
            logger.error(f"Playwright JSON fetch failed: {e}")
            return None
        finally:
            page.close()

    def _playwright_html(self, url: str, wait: int = 5) -> Optional[str]:
        """Fetch full HTML content via Playwright."""
        self._ensure_playwright()
        page = self._pw_context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(wait * 1000)
            return page.content()
        except Exception as e:
            logger.error(f"Playwright HTML fetch failed: {e}")
            return None
        finally:
            page.close()

    # ── Public API ────────────────────────────────────────────────

    def search(self, query: str) -> list[Anime]:
        """Search for anime by name."""
        data = self._api_get_with_fallback({"m": "search", "q": quote(query)})
        if not data or "data" not in data:
            return []
        return [Anime.from_api(a) for a in data["data"]]

    def get_episodes(self, anime_session: str) -> list[Episode]:
        """
        Get ALL episodes for an anime, handling pagination.

        AnimePahe paginates at 30 eps/page. For long-running shows like
        Bleach (366 eps) or One Piece, we fetch all pages.
        """
        episodes = []
        page_num = 1

        while True:
            data = self._api_get_with_fallback({
                "m": "release",
                "id": anime_session,
                "sort": "episode_asc",
                "page": str(page_num),
            })
            if not data or "data" not in data:
                break

            for ep_data in data["data"]:
                episodes.append(Episode(
                    number=ep_data.get("episode", len(episodes) + 1),
                    session=ep_data.get("session", ""),
                    title=ep_data.get("title", ""),
                    snapshot=ep_data.get("snapshot", ""),
                    filler=ep_data.get("filler", 0) == 1,
                ))

            last_page = data.get("last_page", 1)
            if page_num >= last_page:
                break
            page_num += 1

        return episodes

    def get_episode_page(self, anime_session: str, page_num: int = 1) -> Optional[dict]:
        """Get a single page of episode data (for lazy loading)."""
        return self._api_get_with_fallback({
            "m": "release",
            "id": anime_session,
            "sort": "episode_asc",
            "page": str(page_num),
        })

    def get_episode_session(self, anime_session: str, episode_num: int) -> Optional[str]:
        """Get the session ID for a specific episode number."""
        first_page = self.get_episode_page(anime_session, 1)
        if not first_page:
            return None

        per_page = first_page.get("per_page", 30)
        total = first_page.get("total", 0)

        if episode_num < 1 or episode_num > total:
            return None

        page_num = ((episode_num - 1) // per_page) + 1
        idx = (episode_num - 1) % per_page

        if page_num == 1:
            page_data = first_page
        else:
            page_data = self.get_episode_page(anime_session, page_num)

        if not page_data:
            return None

        eps = page_data.get("data", [])
        if idx < len(eps):
            return eps[idx].get("session")
        return None

    def get_sources(self, anime_session: str, episode_session: str) -> list[Source]:
        """
        Get download sources for an episode.

        This navigates the play page with Playwright and extracts the
        dropdown links (which point to pahe.win).
        """
        self._ensure_playwright()
        base = self._base_url or MIRRORS[0]
        play_url = f"{base}/play/{anime_session}/{episode_session}"

        page = self._pw_context.new_page()
        try:
            page.goto(play_url, wait_until="domcontentloaded", timeout=60_000)

            # Wait for quality dropdown links to appear
            try:
                page.wait_for_selector(
                    'a.dropdown-item[target="_blank"]', timeout=30_000
                )
            except Exception:
                page.wait_for_timeout(5000)

            # Extract all download links with metadata
            items = page.eval_on_selector_all(
                'a.dropdown-item[target="_blank"]',
                """els => els.map(e => ({
                    href: e.href,
                    text: e.textContent.trim()
                }))""",
            ) or []

            sources = []
            for item in items:
                href = item.get("href", "")
                text = item.get("text", "")

                # Parse quality from text like "Judas · 1080p (137MB) BD"
                quality_match = re.search(r"(\d{3,4})p", text)
                quality = quality_match.group(0) if quality_match else "unknown"

                # Parse audio
                audio = "eng" if "eng" in text.lower() else "jpn"

                # Parse fansub group
                fansub = text.split("·")[0].strip() if "·" in text else ""

                # Parse size
                size_match = re.search(r"\((\d+(?:\.\d+)?\s*[MG]B)\)", text)
                size = size_match.group(1) if size_match else ""

                if href:
                    sources.append(Source(
                        url=href,
                        quality=quality,
                        audio=audio,
                        fansub=fansub,
                        size=size,
                    ))

            return sources

        except Exception as e:
            logger.error(f"Failed to get sources: {e}")
            return []
        finally:
            page.close()

    def close(self):
        """Clean up browser resources."""
        if self._pw_context:
            try:
                self._pw_context.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass

    @property
    def pw_context(self):
        """Expose the Playwright browser context for sharing with Downloader.

        Ensures Playwright is initialized and Cloudflare is cleared first.
        """
        self._ensure_playwright()
        return self._pw_context