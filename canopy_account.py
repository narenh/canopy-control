"""Canopy Accounts for yt-speed: who is calling, from the iOS app's bearer token.

A Python port of readToken and whoIs from the account service's client/canopy-account.js,
for bearer tokens only (this server has no web pages, so no canopy_session cookie and no
X-Canopy-Site-Host / renewCookie handling).

    canopy = CanopyAccount(url="https://account.canopysf.com", key="cnp_...")
    token = read_token(request.headers.get("authorization"))   # or None
    answer = await canopy.who_is(token)   # {"person": {...} | None, "unverified": bool}

who_is asks GET {url}/api/session with the site key and the token. Each answer is cached
for 60 s by the SHA-256 of the token. If the account service can't be reached or answers
non-2xx, a cached answer up to 15 minutes old stands in; past that it raises Unreachable.
"""
import asyncio, hashlib, logging, re, time

import aiohttp

DEFAULT_URL = "https://account.canopysf.com"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
BEARER_RE = re.compile(r"^Bearer\s+(.*)$", re.IGNORECASE | re.DOTALL)
CACHE_S = 60          # how long an answer is trusted as is
STALE_OK_S = 15 * 60  # how long it may stand in while the account service is unreachable
CACHE_MAX = 5000      # cleared outright past this many entries
TIMEOUT_S = 10
log = logging.getLogger("canopy-account")


class Unreachable(Exception):
    """The account service couldn't be asked (or refused the site key), and no recent answer is cached."""


def read_token(authorization: str | None) -> str | None:
    """The session token from an `Authorization: Bearer <token>` header, or None. A Bearer header
    that isn't shaped like a token means nobody; other schemes are ignored (also nobody here)."""
    m = BEARER_RE.match((authorization or "").strip())
    if not m:
        return None
    token = m.group(1)
    return token if TOKEN_RE.match(token) else None


class CanopyAccount:
    def __init__(self, url: str, key: str):
        if not url or not key:
            raise ValueError("canopy-account: url and key are both required")
        self.base = url.rstrip("/")
        self.key = key
        self.cache: dict[str, tuple[dict, float]] = {}  # sha256(token) -> (answer, at)
        self.clock = time.monotonic  # replaceable in tests
        self._session: aiohttp.ClientSession | None = None
        self._session_loop: asyncio.AbstractEventLoop | None = None

    async def _http(self) -> aiohttp.ClientSession:
        loop = asyncio.get_running_loop()
        if self._session is None or self._session.closed or self._session_loop is not loop:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S))
            self._session_loop = loop
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _ask(self, token: str) -> dict:
        """The account service's raw answer for this token. Raises on anything but a 2xx JSON object."""
        http = await self._http()
        # No X-Canopy-Site-Host: that's only for cookie requests (it asks for a renewed cookie).
        headers = {"Authorization": f"Bearer {self.key}", "X-Canopy-Session": token}
        async with http.get(f"{self.base}/api/session", headers=headers) as r:
            if r.status == 401:
                log.error("the account service rejected CANOPY_ACCOUNT_KEY (401): check the key in the Sites tab")
            if not 200 <= r.status < 300:
                raise RuntimeError(f"account service answered {r.status}")
            body = await r.json(content_type=None)
        if not isinstance(body, dict):
            raise RuntimeError("account service answer isn't a JSON object")
        person = body.get("person")
        if person is not None and not (isinstance(person, dict) and isinstance(person.get("id"), str) and person["id"]):
            raise RuntimeError("account service answer has a person without an id")
        return body

    async def who_is(self, token: str | None) -> dict:
        """{"person": dict | None, "unverified": bool} for this token."""
        if not token:
            return {"person": None, "unverified": False}
        tid = hashlib.sha256(token.encode()).hexdigest()
        hit = self.cache.get(tid)
        now = self.clock()
        if hit and now - hit[1] < CACHE_S:
            return hit[0]
        try:
            body = await self._ask(token)
        except Exception as ex:
            if hit and now - hit[1] < STALE_OK_S:
                log.warning("account service unreachable (%s); using a cached answer", ex)
                return hit[0]
            log.error("account service unreachable: %s", ex)
            raise Unreachable(str(ex)) from ex
        person = body.get("person") or None
        answer = {"person": person, "unverified": not person and bool(body.get("unverified"))}
        if len(self.cache) > CACHE_MAX:
            self.cache.clear()
        self.cache[tid] = (answer, self.clock())
        return answer
