"""yt-speed: hold a YouTube Lounge session to one TV and expose a tiny HTTP API.

POST /captions/{on|off}     - manual caption toggle
POST /pair {"code": "..."}  - one-time pairing with the TV's "Link with TV code"
POST /speed/{x}             - x in 1, 1.25, 1.5, 1.75, 2; captions on above 1, off at 1
GET  /                     - web UI
GET  /status                - connection, current video, speed
"""
import asyncio, json, logging, os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pyytlounge import YtLoungeApi, EventListener

AUTH_FILE = Path(os.environ.get("AUTH_FILE", "/data/auth.json"))
CAPTION_LANG = os.environ.get("CAPTION_LANG", "en")
SPEEDS = {1.0, 1.25, 1.5, 1.75, 2.0}
log = logging.getLogger("yt-speed")
logging.basicConfig(level=logging.INFO)


class State(EventListener):
    video_id: str | None = None
    speed: float = 1.0
    captions: bool = False

    async def now_playing_changed(self, e):
        if e.video_id:
            self.video_id = e.video_id

    async def playback_speed_changed(self, e):
        self.speed = e.playback_speed

    async def subtitles_track_changed(self, e):
        self.captions = bool(e.language_code)


state = State()
api = YtLoungeApi("Speed Remote", state)


def save_auth():
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(api.store_auth_state()))


async def session_loop():
    """Keep connected + subscribed. Reconnects when the TV app closes or the token expires."""
    while True:
        try:
            if not api.paired():
                await asyncio.sleep(5)
                continue
            if not api.linked():
                await api.refresh_auth()
                save_auth()
            if await api.connect():
                log.info("connected")
                await api.get_now_playing()
                await api.subscribe()  # blocks until the session drops
                log.info("session ended")
        except Exception as ex:
            log.warning("session error: %s", ex)
        await asyncio.sleep(10)  # TV off / app closed: retry quietly


@asynccontextmanager
async def lifespan(_):
    async with api:
        if AUTH_FILE.exists():
            api.load_auth_state(json.loads(AUTH_FILE.read_text()))
        task = asyncio.create_task(session_loop())
        yield
        task.cancel()


app = FastAPI(lifespan=lifespan)


class Pair(BaseModel):
    code: str


@app.post("/pair")
async def pair(body: Pair):
    if not await api.pair(body.code.replace(" ", "")):
        raise HTTPException(400, "pairing failed")
    save_auth()
    return {"paired": True, "screen": api.screen_name}


@app.post("/speed/{speed}")
async def set_speed(speed: float):
    if speed not in SPEEDS:
        raise HTTPException(400, f"speed must be one of {sorted(SPEEDS)}")
    if not api.connected():
        raise HTTPException(503, "not connected (is YouTube open on the TV?)")
    await api.set_playback_speed(speed)
    if state.video_id:
        await api.set_closed_captions(CAPTION_LANG if speed > 1 else None, state.video_id)
        state.captions = speed > 1
    return {"speed": speed, "captions": speed > 1, "video_id": state.video_id}


@app.post("/captions/{mode}")
async def set_captions(mode: str):
    if mode not in ("on", "off"):
        raise HTTPException(400, "mode must be on or off")
    if not api.connected():
        raise HTTPException(503, "not connected (is YouTube open on the TV?)")
    if not state.video_id:
        raise HTTPException(409, "no video playing")
    await api.set_closed_captions(CAPTION_LANG if mode == "on" else None, state.video_id)
    state.captions = mode == "on"
    return {"captions": mode == "on"}


@app.get("/status")
async def status():
    return {
        "paired": api.paired(),
        "connected": api.connected(),
        "video_id": state.video_id,
        "speed": state.speed,
        "captions": state.captions,
    }


@app.get("/")
async def ui():
    return FileResponse(Path(__file__).with_name("index.html"))
