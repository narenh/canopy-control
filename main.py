"""yt-speed: hold YouTube Lounge sessions to TVs, per Canopy Account, and expose a tiny HTTP API.

Every route needs `Authorization: Bearer <Canopy session token>` (the iOS app's sign-in). Each
person sees and controls only the TVs they paired; two people can pair the same TV, each with
their own Lounge session. Up to MAX_DEVICES TVs per person. There is no web UI.

GET    /devices                   - the caller's TVs with connection/playback state
POST   /pair {"code": "..."}      - pair another TV via its "Link with TV code"
DELETE /devices/{id}              - forget a TV
POST   /devices/{id}/speed/{x}    - x in 1, 1.25, 1.5, 1.75, 2; captions on above 1, off at 1
POST   /devices/{id}/captions/{on|off}   - last of slider/toggle wins, re-applied to each new video

Errors are JSON {"detail": "<sentence>"}, plus "reason" for: 401 signed_out, 403 email_unverified,
503 account_unreachable, 409 too_many_devices.

Env: CANOPY_ACCOUNT_KEY (required; the site key from the Account Manager's Sites tab),
CANOPY_ACCOUNT_URL (default https://account.canopysf.com), DATA_DIR (default /data),
CAPTION_LANG (default en).
"""
import asyncio, json, logging, os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pyytlounge import YtLoungeApi, EventListener
from pyytlounge.models import State

import canopy_account

DATA = Path(os.environ.get("DATA_DIR", "/data"))
DEVICES_FILE = DATA / "devices.json"
LEGACY_AUTH = DATA / "auth.json"  # single-TV file from earlier versions, no owner: ignored
CAPTION_LANG = os.environ.get("CAPTION_LANG", "en")
CAPTION_WAIT = 3.0  # seconds to wait for the TV to report a caption change
SPEEDS = {1.0, 1.25, 1.5, 1.75, 2.0}
MAX_DEVICES = 10  # TVs per person
CANOPY_ACCOUNT_URL = (os.environ.get("CANOPY_ACCOUNT_URL") or "").strip() or canopy_account.DEFAULT_URL
CANOPY_ACCOUNT_KEY = (os.environ.get("CANOPY_ACCOUNT_KEY") or "").strip()
log = logging.getLogger("yt-speed")
logging.basicConfig(level=logging.INFO)
canopy = canopy_account.CanopyAccount(CANOPY_ACCOUNT_URL, CANOPY_ACCOUNT_KEY) if CANOPY_ACCOUNT_KEY else None


class Device(EventListener):
    """One paired TV: its Lounge session, reconnect loop, and last-known playback state."""

    def __init__(self, owner: str, name: str = "TV"):
        self.owner = owner  # Canopy person id of whoever paired it
        self.name = name
        self.api = YtLoungeApi("Speed Remote", self)
        self.task: asyncio.Task | None = None
        self.video_id: str | None = None
        self.speed = 1.0
        self.captions = False  # only ever set from the TV's onSubtitlesTrackChanged
        self.want_captions: bool | None = None  # last control wins: slider (on above 1x) or toggle
        self._captions_changed = asyncio.Event()
        self._speed_changed = asyncio.Event()
        self._auto_captioned: str | None = None  # last video want_captions was applied to

    # --- events from the TV
    async def now_playing_changed(self, e):
        if e.video_id:
            self.video_id = e.video_id
        if e.state == State.Playing:
            await self._auto_caption()

    async def playback_state_changed(self, e):
        if e.state == State.Playing:
            await self._auto_caption()

    async def playback_speed_changed(self, e):
        self.speed = e.playback_speed
        self._speed_changed.set()

    async def set_speed(self, speed: float):
        """Set the speed and, if it changes, wait for the TV to apply it: a caption command
        sent while the TV is still changing speed gets dropped."""
        changing = speed != self.speed
        self._speed_changed.clear()
        await self.api.set_playback_speed(speed)
        if changing:
            try:
                await asyncio.wait_for(self._speed_changed.wait(), CAPTION_WAIT)
            except asyncio.TimeoutError:
                log.warning("%s: TV didn't report a speed change to %s", self.name, speed)
        self.speed = speed  # callers render the response, even if the TV's event is late

    async def subtitles_track_changed(self, e):
        log.info("%s captions: video=%s lang=%r kind=%r", self.name, e.video_id, e.language_code, e.kind)
        self.captions = bool(e.language_code)
        self._captions_changed.set()

    # --- captions
    async def _send_captions(self, on: bool):
        if on:
            await self.api.set_closed_captions(CAPTION_LANG, self.video_id)
        else:
            # pyytlounge sends languageCode="" for off, which the TV ignores. YouTube's own remote
            # (player remote.js) sends setSubtitlesTrack with only the videoId.
            await self.api._command("setSubtitlesTrack", {"videoId": self.video_id})

    async def _auto_caption(self):
        """The TV keeps the speed across videos but not captions, so re-apply the last choice per video."""
        if self.want_captions is None or not self.video_id or self.video_id == self._auto_captioned:
            return
        self._auto_captioned = self.video_id
        try:
            # Don't wait for the change here: the TV's reply arrives through this same event loop.
            await self._send_captions(self.want_captions)
        except Exception as ex:
            log.warning("%s auto-captions failed: %s", self.name, ex)

    async def set_captions(self, on: bool):
        """Ask the TV for captions; if that's a change, wait for the TV to confirm it."""
        self.want_captions = on
        self._auto_captioned = self.video_id
        self._captions_changed.clear()
        await self._send_captions(on)
        if self.captions != on:
            try:
                await asyncio.wait_for(self._captions_changed.wait(), CAPTION_WAIT)
            except asyncio.TimeoutError:
                log.warning("%s: TV didn't report a caption change for %s", self.name, self.video_id)

    # --- lifecycle
    @property
    def id(self) -> str:
        return self.api.auth.screen_id

    async def start(self):
        await self.api.__aenter__()
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
        await self.api.close()

    async def _loop(self):
        """Keep connected + subscribed. Reconnects when the TV app closes or the token expires."""
        while True:
            try:
                if not self.api.linked():
                    await self.api.refresh_auth()
                    save_devices()
                if await self.api.connect():
                    if self.api.screen_name and self.name != self.api.screen_name:
                        self.name = self.api.screen_name
                        save_devices()
                    log.info("connected to %s", self.name)
                    await self.api.get_now_playing()
                    await self.api.subscribe()  # blocks until the session drops
                    log.info("session ended: %s", self.name)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                log.warning("%s session error: %s", self.name, ex)
            await asyncio.sleep(10)  # TV off / app closed: retry quietly

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "connected": self.api.connected(),
                "video_id": self.video_id, "speed": self.speed, "captions": self.captions}


devices: dict[tuple[str, str], Device] = {}  # (owner, screen_id) -> Device


def save_devices():
    DATA.mkdir(parents=True, exist_ok=True)
    # api.store_auth_state() writes a format load_auth_state() rejects (pyytlounge 3.4 bug)
    DEVICES_FILE.write_text(json.dumps(
        [{"owner": d.owner, "name": d.name, "auth": d.api.auth.serialize()} for d in devices.values()],
        indent=1))


def _normalize_auth(data: dict) -> dict:
    if "version" in data:
        return data
    return {"version": 0, "screenId": data.get("screenId"),  # old store_auth_state() format
            "loungeIdToken": data.get("lounge_id_token"),
            "refreshToken": data.get("refresh_token"), "expiry": None}


def load_devices() -> tuple[list[Device], int]:
    """The saved TVs, and how many ownerless (pre-accounts) entries were dropped."""
    entries = json.loads(DEVICES_FILE.read_text()) if DEVICES_FILE.exists() else []
    if not DEVICES_FILE.exists() and LEGACY_AUTH.exists():
        log.warning("ignoring %s from before Canopy Accounts: re-pair the TV from the app", LEGACY_AUTH)
    out, dropped = [], 0
    for entry in entries:
        owner = entry.get("owner")
        if not isinstance(owner, str) or not owner:
            dropped += 1
            continue
        d = Device(owner, entry.get("name", "TV"))
        try:
            d.api.load_auth_state(_normalize_auth(entry["auth"]))
            out.append(d)
        except Exception as ex:
            log.warning("skipping unreadable device entry (re-pair needed): %s", ex)
    if dropped:
        log.warning("dropped %d TV(s) paired before Canopy Accounts (no owner): re-pair them from the app",
                    dropped)
    return out, dropped


@asynccontextmanager
async def lifespan(_):
    if canopy is None:
        log.critical("CANOPY_ACCOUNT_KEY is not set: refusing to start. Make a site in the Account "
                     "Manager's Sites tab and set its key as CANOPY_ACCOUNT_KEY.")
        raise RuntimeError("CANOPY_ACCOUNT_KEY is not set")
    log.info("Canopy Accounts at %s", canopy.base)
    loaded, dropped = load_devices()
    for d in loaded:
        devices[(d.owner, d.id)] = d
    if dropped:
        save_devices()  # rewrite without the ownerless entries
    for d in devices.values():
        await d.start()
    yield
    for d in devices.values():
        await d.stop()
    await canopy.close()


app = FastAPI(lifespan=lifespan)


class ApiError(HTTPException):
    """An HTTPException that also says why, as a machine-readable `reason`."""

    def __init__(self, status: int, detail: str, reason: str, headers: dict | None = None):
        super().__init__(status, detail, headers)
        self.reason = reason


@app.exception_handler(ApiError)
async def api_error(_: Request, ex: ApiError):
    return JSONResponse({"detail": ex.detail, "reason": ex.reason}, ex.status_code, headers=ex.headers)


async def require_person(request: Request) -> dict:
    """The signed-in Canopy person calling, from `Authorization: Bearer <token>`. Fails closed."""
    signed_out = ApiError(401, "Sign in with your Canopy Account.", "signed_out", {"WWW-Authenticate": "Bearer"})
    token = canopy_account.read_token(request.headers.get("authorization"))
    if not token:
        raise signed_out
    if canopy is None:  # only if started without the lifespan check
        raise ApiError(503, "Canopy Accounts isn't set up on this server.", "account_unreachable")
    try:
        answer = await canopy.who_is(token)
    except canopy_account.Unreachable:
        raise ApiError(503, "Canopy Accounts could not be reached. Try again in a minute.",
                       "account_unreachable")
    person = answer["person"]
    if answer["unverified"] or (person and person.get("emailVerified") is False):
        raise ApiError(403, "Confirm your email with Canopy Accounts first.", "email_unverified")
    if not person:
        raise signed_out
    return person


def get(owner: str, device_id: str, need_video=False) -> Device:
    d = devices.get((owner, device_id))
    if not d:
        raise HTTPException(404, "unknown device")
    if not d.api.connected():
        raise HTTPException(503, f"{d.name} not connected (is YouTube open on it?)")
    if need_video and not d.video_id:
        raise HTTPException(409, "no video playing")
    return d


class Pair(BaseModel):
    code: str


@app.get("/devices")
async def list_devices(person: dict = Depends(require_person)):
    return [d.info() for d in devices.values() if d.owner == person["id"]]


@app.post("/pair")
async def pair(body: Pair, person: dict = Depends(require_person)):
    owner = person["id"]
    d = Device(owner)
    await d.api.__aenter__()
    if not await d.api.pair(body.code.replace(" ", "")):
        await d.api.close()
        raise HTTPException(400, "pairing failed")
    d.name = d.api.screen_name or "TV"
    key = (owner, d.id)
    # Checked after pairing (only then is the TV known), so re-pairing a TV at the limit works.
    # No await between this check and the insert, so concurrent pairs can't overshoot.
    if key not in devices and sum(x.owner == owner for x in devices.values()) >= MAX_DEVICES:
        await d.api.close()
        raise ApiError(409, f"You can pair up to {MAX_DEVICES} TVs. Remove one first.", "too_many_devices")
    old = devices.get(key)  # re-pairing the same TV replaces this person's entry
    devices[key] = d
    if old:
        await old.stop()
    d.task = asyncio.create_task(d._loop())
    save_devices()
    return d.info()


@app.delete("/devices/{device_id}")
async def remove(device_id: str, person: dict = Depends(require_person)):
    d = devices.pop((person["id"], device_id), None)
    if not d:
        raise HTTPException(404, "unknown device")
    await d.stop()
    save_devices()
    return {"removed": device_id}


@app.post("/devices/{device_id}/speed/{speed}")
async def set_speed(device_id: str, speed: float, person: dict = Depends(require_person)):
    if speed not in SPEEDS:
        raise HTTPException(400, f"speed must be one of {sorted(SPEEDS)}")
    d = get(person["id"], device_id)
    await d.set_speed(speed)
    if d.video_id:
        await d.set_captions(speed > 1)
    else:
        d.want_captions = speed > 1  # applied when the next video starts
    return d.info()


@app.post("/devices/{device_id}/captions/{mode}")
async def set_captions(device_id: str, mode: str, person: dict = Depends(require_person)):
    if mode not in ("on", "off"):
        raise HTTPException(400, "mode must be on or off")
    d = get(person["id"], device_id, need_video=True)
    await d.set_captions(mode == "on")
    if d.captions != (mode == "on"):
        hint = f" (does this video have '{CAPTION_LANG}' captions?)" if mode == "on" else ""
        # not 502: Cloudflare replaces 502 bodies with its own page, hiding the detail
        raise HTTPException(409, f"{d.name} didn't turn captions {mode}{hint}")
    return d.info()
