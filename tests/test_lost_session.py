"""A speed or caption command that doesn't reach the TV is a 503 that starts a fresh session, not a 500.

    python -I tests/test_lost_session.py [repo]

The case that 500ed: YouTube had dropped our Lounge session without our subscription noticing (the
app showed an old video). A speed tap: pyytlounge's command finds the session gone (400 Unknown SID),
forgets it and returns False; the caption command after it then raises NotConnectedException.
"""
import asyncio, os, sys, tempfile, types
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(REPO))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("CANOPY_ACCOUNT_KEY", "cnp_test")
import aiohttp  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pyytlounge.exceptions import NotConnectedException  # noqa: E402
from yarl import URL  # noqa: E402

main.CAPTION_WAIT = 0.1


async def no_title(vid):
    return None, None


main.titles.fetch = no_title
main.shorts_check.fetch = lambda vid: asyncio.sleep(0, False)  # never youtube.com: no Shorts here
passed = 0


def check(name, cond, info=""):
    global passed
    assert cond, f"{name}: {info}"
    passed += 1


class DroppedLounge:
    """Thinks it's connected until a command tells it otherwise, like pyytlounge's _command."""
    def __init__(self):
        self.sid = True
        self.fail: Exception | None = None  # raise this from the next commands instead
        self.sent = []
        self.auth = types.SimpleNamespace(screen_id="scr-1")

    def connected(self): return self.sid

    async def _command(self, cmd, params):
        if self.fail:
            raise self.fail
        if not self.sid:
            raise NotConnectedException("Not connected")
        self.sent.append(cmd)
        self.sid = False  # 400 Unknown SID: _connection_lost(), then False
        return False

    async def set_playback_speed(self, s): return await self._command("setPlaybackSpeed", {"playbackSpeed": s})
    async def set_closed_captions(self, lang, vid): return await self._command("setSubtitlesTrack", {"languageCode": lang})
    async def close(self): pass


loops = []


async def fake_loop(self):
    loops.append(self)
    await asyncio.sleep(3600)


main.Device._loop = fake_loop
OWNER = "owner-1"
main.app.dependency_overrides[main.require_person] = lambda: {"id": OWNER}
d = main.Device(OWNER, "Bedroom")
d.api = DroppedLounge()
d.video_id = "abcdefghijk"
d.speed = 1.5


async def start():
    d.task = asyncio.create_task(d._loop())


with TestClient(main.app, raise_server_exceptions=False) as c:
    main.devices[(OWNER, d.id)] = d  # after startup, which would start() it
    c.portal.call(start)
    first = d.task

    r = c.post(f"/devices/{d.id}/speed/1")
    check("speed on a dropped session: 503, not 500", r.status_code == 503, (r.status_code, r.text))
    check("with a JSON detail naming the TV", r.json()["detail"].startswith("Bedroom isn't answering"), r.text)
    check("only the speed command was tried", d.api.sent == ["setPlaybackSpeed"], d.api.sent)
    check("a fresh session was started", d.task is not first and first.cancelled(), d.task)
    check("the speed isn't claimed", d.speed == 1.5, d.speed)

    r = c.post(f"/devices/{d.id}/speed/1")
    check("while it reconnects: the not-connected 503", r.status_code == 503 and "not connected" in r.json()["detail"], r.text)

    # YouTube answers with an error pyytlounge doesn't recognize: raise_for_status raises
    d.api.sid, d.api.fail = True, aiohttp.ClientResponseError(
        aiohttp.RequestInfo(URL("https://www.youtube.com/api/lounge/bc/bind"), "POST", {}), (), status=410, message="Gone")
    d._resubscribed_at -= main.RESUBSCRIBE_GAP_S
    r = c.post(f"/devices/{d.id}/captions/on")
    check("captions refused by YouTube: 503, not 500", r.status_code == 503, (r.status_code, r.text))
    check("and a fresh session again", len(loops) == 3, loops)

    d.api.fail = aiohttp.ClientConnectionError("network down")
    r = c.post(f"/devices/{d.id}/speed/2")
    check("network error: 503 (within the gap, no second restart)", r.status_code == 503 and len(loops) == 3,
          (r.status_code, loops))
    d.task.cancel()

print(f"{passed} checks passed")
