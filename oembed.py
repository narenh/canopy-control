"""Video titles and channels, from YouTube's public oEmbed endpoint (no API key).

    titles = Titles()               # or Titles(fetch=fake) in tests
    titles.want("dQw4w9WgXcQ")      # fetch it in the background unless cached or in flight; never blocks
    titles.get("dQw4w9WgXcQ")       # (title, channel) as cached now; (None, None) if not known

GET https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=<id>&format=json, fields
`title` and `author_name`. Only ids matching VIDEO_RE are fetched. Answers are kept in an LRU of
CACHE_MAX ids. A failure (non-200, e.g. 401/403/404 for private or unembeddable videos, a timeout,
a body that isn't JSON) is logged at INFO and cached as (None, None) for FAIL_S, then tried again
the next time the id is wanted.
"""
import asyncio, logging, re, time
from collections import OrderedDict
from typing import Awaitable, Callable

import aiohttp

URL = "https://www.youtube.com/oembed"
VIDEO_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
TIMEOUT_S = 5
CACHE_MAX = 500   # video ids
FAIL_S = 10 * 60  # how long a failure is cached before the id is fetched again
NONE = (None, None)
log = logging.getLogger("oembed")

Fetch = Callable[[str], Awaitable[tuple[str | None, str | None]]]


class Failed(Exception):
    """oEmbed didn't give a usable answer for a video."""


def _text(v) -> str | None:
    return (v.strip() or None) if isinstance(v, str) else None


class Titles:
    def __init__(self, fetch: Fetch | None = None):
        self.fetch: Fetch = fetch or self._fetch  # (video_id) -> (title, channel); raises on failure
        self.clock = time.monotonic  # replaceable in tests
        # video_id -> ((title, channel), expires at, or None for an answer kept until evicted)
        self.cache: OrderedDict[str, tuple[tuple[str | None, str | None], float | None]] = OrderedDict()
        self._pending: dict[str, asyncio.Task] = {}
        self._session: aiohttp.ClientSession | None = None
        self._session_loop: asyncio.AbstractEventLoop | None = None

    def get(self, video_id: str | None) -> tuple[str | None, str | None]:
        entry = self.cache.get(video_id) if video_id else None
        if entry is None:
            return NONE
        self.cache.move_to_end(video_id)
        return entry[0]

    def want(self, video_id: str | None):
        """Start a background fetch for this id, unless it's not a video id, is cached (and not an
        expired failure), or is already being fetched. Needs a running event loop; without one, does
        nothing. Never raises."""
        try:
            if not isinstance(video_id, str) or not VIDEO_RE.match(video_id) or video_id in self._pending:
                return
            entry = self.cache.get(video_id)
            if entry is not None and (entry[1] is None or self.clock() < entry[1]):
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            task = loop.create_task(self._run(video_id))
            self._pending[video_id] = task
            task.add_done_callback(lambda _t, v=video_id: self._pending.pop(v, None))
        except Exception:
            log.exception("couldn't start a title fetch for %s", video_id)

    async def _run(self, video_id: str):
        try:
            title, channel = await self.fetch(video_id)
            value, expires = (_text(title), _text(channel)), None
            if value == NONE:
                raise Failed("no title or author_name")
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.info("no title for video %s: %s", video_id, str(ex) or type(ex).__name__)
            value, expires = NONE, self.clock() + FAIL_S
        self.cache[video_id] = (value, expires)
        self.cache.move_to_end(video_id)
        while len(self.cache) > CACHE_MAX:
            self.cache.popitem(last=False)

    async def _http(self) -> aiohttp.ClientSession:
        loop = asyncio.get_running_loop()
        if self._session is None or self._session.closed or self._session_loop is not loop:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S))
            self._session_loop = loop
        return self._session

    async def _fetch(self, video_id: str) -> tuple[str | None, str | None]:
        http = await self._http()
        params = {"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"}
        async with http.get(URL, params=params) as r:
            if r.status != 200:
                raise Failed(f"oEmbed answered {r.status}")
            try:
                data = await r.json(content_type=None)
            except ValueError as ex:
                raise Failed("oEmbed answer isn't JSON") from ex
        if not isinstance(data, dict):
            raise Failed("oEmbed answer isn't an object")
        return data.get("title"), data.get("author_name")

    async def drain(self):
        """Wait for the fetches in flight (tests)."""
        while self._pending:
            await asyncio.gather(*list(self._pending.values()), return_exceptions=True)

    async def close(self):
        for t in list(self._pending.values()):
            t.cancel()
        await asyncio.gather(*list(self._pending.values()), return_exceptions=True)
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
