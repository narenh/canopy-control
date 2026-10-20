"""yt-speed: hold YouTube Lounge sessions to one or more TVs and expose a tiny HTTP API.

GET    /                          - web UI
GET    /devices                   - paired TVs with connection/playback state
POST   /pair {"code": "..."}      - pair another TV via its "Link with TV code"
DELETE /devices/{id}              - forget a TV
POST   /devices/{id}/speed/{x}    - x in 1, 1.25, 1.5, 1.75, 2; captions on above 1, off at 1
POST   /devices/{id}/captions/{on|off}
"""
import asyncio, json, logging, os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pyytlounge import YtLoungeApi, EventListener

DATA = Path(os.environ.get("DATA_DIR", "/data"))
DEVICES_FILE = DATA / "devices.json"
LEGACY_AUTH = DATA / "auth.json"  # single-TV file from earlier versions
CAPTION_LANG = os.environ.get("CAPTION_LANG", "en")
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
        self.captions = False

    # --- events from the TV
    async def now_playing_changed(self, e):
        if e.video_id:
            self.video_id = e.video_id

    async def playback_speed_changed(self, e):
        self.speed = e.playback_speed

    async def subtitles_track_changed(self, e):
        self.captions = bool(e.language_code)

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
    await d.api.set_playback_speed(speed)
    d.speed = speed  # don't wait for the TV's event; callers render the response
    if d.video_id:
        await d.api.set_closed_captions(CAPTION_LANG if speed > 1 else None, d.video_id)
        d.captions = speed > 1
    return d.info()


@app.post("/devices/{device_id}/captions/{mode}")
async def set_captions(device_id: str, mode: str):
    if mode not in ("on", "off"):
        raise HTTPException(400, "mode must be on or off")
    d = get(device_id, need_video=True)
    await d.api.set_closed_captions(CAPTION_LANG if mode == "on" else None, d.video_id)
    d.captions = mode == "on"
    return d.info()


@app.get("/")
async def ui():
    return FileResponse(Path(__file__).with_name("index.html"))
