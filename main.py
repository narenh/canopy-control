"""yt-speed: hold YouTube Lounge sessions to one or more TVs and expose a tiny HTTP API.

GET    /                          - web UI
GET    /devices                   - paired TVs with connection/playback state
POST   /pair {"code": "..."}      - pair another TV via its "Link with TV code"
DELETE /devices/{id}              - forget a TV
POST   /devices/{id}/speed/{x}    - x in 1, 1.25, 1.5, 1.75, 2; captions on above 1, off at 1
POST   /devices/{id}/captions/{on|off}   - last of slider/toggle wins, re-applied to each new video
"""
import asyncio, json, logging, os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pyytlounge import YtLoungeApi, EventListener
from pyytlounge.models import State

DATA = Path(os.environ.get("DATA_DIR", "/data"))
DEVICES_FILE = DATA / "devices.json"
LEGACY_AUTH = DATA / "auth.json"  # single-TV file from earlier versions
CAPTION_LANG = os.environ.get("CAPTION_LANG", "en")
CAPTION_WAIT = 3.0  # seconds to wait for the TV to report a caption change
SPEEDS = {1.0, 1.25, 1.5, 1.75, 2.0}
log = logging.getLogger("yt-speed")
logging.basicConfig(level=logging.INFO)


class Device(EventListener):
    """One paired TV: its Lounge session, reconnect loop, and last-known playback state."""

    def __init__(self, name: str = "TV"):
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


devices: dict[str, Device] = {}


def save_devices():
    DATA.mkdir(parents=True, exist_ok=True)
    # api.store_auth_state() writes a format load_auth_state() rejects (pyytlounge 3.4 bug)
    DEVICES_FILE.write_text(json.dumps(
        [{"name": d.name, "auth": d.api.auth.serialize()} for d in devices.values()], indent=1))


def _normalize_auth(data: dict) -> dict:
    if "version" in data:
        return data
    return {"version": 0, "screenId": data.get("screenId"),  # old store_auth_state() format
            "loungeIdToken": data.get("lounge_id_token"),
            "refreshToken": data.get("refresh_token"), "expiry": None}


def load_devices() -> list[Device]:
    if DEVICES_FILE.exists():
        entries = json.loads(DEVICES_FILE.read_text())
    elif LEGACY_AUTH.exists():
        entries = [{"name": "TV", "auth": json.loads(LEGACY_AUTH.read_text())}]
    else:
        entries = []
    out = []
    for entry in entries:
        d = Device(entry.get("name", "TV"))
        try:
            d.api.load_auth_state(_normalize_auth(entry["auth"]))
            out.append(d)
        except Exception as ex:
            log.warning("skipping unreadable device entry (re-pair needed): %s", ex)
    return out


@asynccontextmanager
async def lifespan(_):
    for d in load_devices():
        devices[d.id] = d
        await d.start()
    if devices and not DEVICES_FILE.exists():
        save_devices()  # migrate legacy auth.json
    yield
    for d in devices.values():
        await d.stop()


app = FastAPI(lifespan=lifespan)


def get(device_id: str, need_video=False) -> Device:
    d = devices.get(device_id)
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
async def list_devices():
    return [d.info() for d in devices.values()]


@app.post("/pair")
async def pair(body: Pair):
    d = Device()
    await d.api.__aenter__()
    if not await d.api.pair(body.code.replace(" ", "")):
        await d.api.close()
        raise HTTPException(400, "pairing failed")
    d.name = d.api.screen_name or "TV"
    if old := devices.pop(d.id, None):  # re-pairing the same TV replaces it
        await old.stop()
    devices[d.id] = d
    d.task = asyncio.create_task(d._loop())
    save_devices()
    return d.info()


@app.delete("/devices/{device_id}")
async def remove(device_id: str):
    d = devices.pop(device_id, None)
    if not d:
        raise HTTPException(404, "unknown device")
    await d.stop()
    save_devices()
    return {"removed": device_id}


@app.post("/devices/{device_id}/speed/{speed}")
async def set_speed(device_id: str, speed: float):
    if speed not in SPEEDS:
        raise HTTPException(400, f"speed must be one of {sorted(SPEEDS)}")
    d = get(device_id)
    await d.set_speed(speed)
    if d.video_id:
        await d.set_captions(speed > 1)
    else:
        d.want_captions = speed > 1  # applied when the next video starts
    return d.info()


@app.post("/devices/{device_id}/captions/{mode}")
async def set_captions(device_id: str, mode: str):
    if mode not in ("on", "off"):
        raise HTTPException(400, "mode must be on or off")
    d = get(device_id, need_video=True)
    await d.set_captions(mode == "on")
    if d.captions != (mode == "on"):
        hint = f" (does this video have '{CAPTION_LANG}' captions?)" if mode == "on" else ""
        # not 502: Cloudflare replaces 502 bodies with its own page, hiding the detail
        raise HTTPException(409, f"{d.name} didn't turn captions {mode}{hint}")
    return d.info()


@app.get("/")
async def ui():
    return FileResponse(Path(__file__).with_name("index.html"))
