"""Whether a video is a YouTube Short, from https://www.youtube.com/shorts/<id> (no API key).

    shorts = Shorts()               # or Shorts(fetch=fake) in tests
    await shorts.is_short("1FLHigSK3TQ")  # True, False, or None when it couldn't tell

YouTube serves a Short at that URL (200) and redirects anything else to /watch (303), so a HEAD
without following redirects tells them apart. That's YouTube's behavior, not a documented API: any
other answer (404 for an unknown id, a timeout) is None, and callers do nothing. Answers are kept in
an LRU of CACHE_MAX ids; None isn't cached, so the next time the id comes up it's asked again.
"""
import asyncio, logging, re
from collections import OrderedDict
from typing import Awaitable, Callable

import aiohttp

URL = "https://www.youtube.com/shorts/"
VIDEO_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
TIMEOUT_S = 5
CACHE_MAX = 500  # video ids
log = logging.getLogger("shorts")

Fetch = Callable[[str], Awaitable[bool | None]]


class Shorts:
    def __init__(self, fetch: Fetch | None = None):
        self.fetch: Fetch = fetch or self._fetch  # (video_id) -> True/False/None
        self.cache: OrderedDict[str, bool] = OrderedDict()
        self._session: aiohttp.ClientSession | None = None
        self._session_loop: asyncio.AbstractEventLoop | None = None

    async def is_short(self, video_id: str | None) -> bool | None:
        if not isinstance(video_id, str) or not VIDEO_RE.match(video_id):
            return None
        if video_id in self.cache:
            self.cache.move_to_end(video_id)
            return self.cache[video_id]
        try:
            answer = await self.fetch(video_id)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.info("couldn't tell whether %s is a Short: %s", video_id, str(ex) or type(ex).__name__)
            return None
        if answer is not None:
            self.cache[video_id] = answer
            while len(self.cache) > CACHE_MAX:
                self.cache.popitem(last=False)
        return answer

    async def _http(self) -> aiohttp.ClientSession:
        loop = asyncio.get_running_loop()
        if self._session is None or self._session.closed or self._session_loop is not loop:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S))
            self._session_loop = loop
        return self._session

    async def _fetch(self, video_id: str) -> bool | None:
        http = await self._http()
        async with http.head(URL + video_id, allow_redirects=False) as r:
            if r.status == 200:
                return True
            if 300 <= r.status < 400:
                return False
            log.info("%s%s answered %d", URL, video_id, r.status)
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
