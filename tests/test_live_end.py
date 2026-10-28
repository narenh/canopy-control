"""Live Activities end when a TV stops playing, after a grace period (main.LIVE_ACTIVITY_END_GRACE_S).

python -I tests/test_live_end.py [repo path, default: this repo]

A fake account service and a fake canopy-apns relay run on 127.0.0.1 (as in test_push.py),
main.LoungeApi is replaced with a fake, the grace period is shortened, and DATA_DIR is a temp
dir. Nothing touches a real TV, YouTube, the account service, the relay or production.
"""
import asyncio, json, logging, os, socket, sys, tempfile, threading, time, types
from pathlib import Path

from aiohttp import web

REPO = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent)  # default: this repo
sys.path.insert(0, REPO)

# ---------- fake account service + relay
TOK = {k: k * 43 for k in "AB"}
A, B = "aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002"
ANSWERS = {TOK["A"]: {"person": {"id": A, "emailVerified": True}}, TOK["B"]: {"person": {"id": B, "emailVerified": True}}}
SITE_KEY, APNS_KEY = "cnp_test_key", "relay_test_key"
relay_hits = []


async def session(req):
    if req.headers.get("Authorization") != f"Bearer {SITE_KEY}":
        return web.json_response({"error": "bad key"}, status=401)
    return web.json_response(ANSWERS.get(req.headers.get("X-Canopy-Session"), {"person": None}))


async def liveactivity(req):
    relay_hits.append(await req.json())
    return web.json_response({"result": "delivered", "reason": None, "apns_id": "x"})


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


ACCT_PORT, RELAY_PORT = free_port(), free_port()
ready = threading.Event()


def serve():
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    for port, route in ((ACCT_PORT, ("GET", "/api/session", session)), (RELAY_PORT, ("POST", "/v1/liveactivity", liveactivity))):
        a = web.Application(); a.router.add_route(*route)
        runner = web.AppRunner(a); loop.run_until_complete(runner.setup())
        loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", port).start())
    ready.set(); loop.run_forever()


threading.Thread(target=serve, daemon=True).start()
ready.wait(5)

DATA = Path(tempfile.mkdtemp())
for k in ("ISPONSORBLOCKTV_CONFIG", "SKIP_HIDDEN_PERSON_IDS", "CANOPY_CONTROL_BUNDLE_ID"):
    os.environ.pop(k, None)
os.environ.update(DATA_DIR=str(DATA), CANOPY_ACCOUNT_URL=f"http://127.0.0.1:{ACCT_PORT}", CANOPY_ACCOUNT_KEY=SITE_KEY,
                  CANOPY_APNS_URL=f"http://127.0.0.1:{RELAY_PORT}", CANOPY_APNS_KEY=APNS_KEY)


class Logs(logging.Handler):
    records = []
    def emit(self, r): Logs.records.append(r)


import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pyytlounge.events import NowPlayingEvent, PlaybackStateEvent  # noqa: E402
from pyytlounge.models import State  # noqa: E402

logging.getLogger("yt-speed").addHandler(Logs())


# ---------- fake Lounge API
class FakeAuth:
    def __init__(self): self.screen_id = None
    def serialize(self): return {"version": 1, "screenId": self.screen_id}


def now_playing(vid, state):
    """What pyytlounge makes of a nowPlaying event (no "state" in it: State.Stopped)."""
    return NowPlayingEvent({**({"videoId": vid} if vid else {}), **({"state": str(state.value)} if state else {})})


class FakeLounge:
    """Codes starting with LIVE connect through _loop (subscribe blocks until `drop` is set) and,
    like the real TV, report `now` (video, state) in a nowPlaying: during connect() when
    `in_bind` (the bind response), else as the answer to get_now_playing(). Others never connect
    through _loop but report connected."""

    def __init__(self, name, listener=None, remote_key=None):
        self.listener, self.auth, self.screen_name, self.closed = listener, FakeAuth(), None, False
        self.live, self.sid, self.drop, self.connects = False, False, None, 0
        self.now, self.in_bind = None, False
    async def __aenter__(self): return self
    async def pair(self, code):
        self.auth.screen_id, self.screen_name = f"scr-{code}", f"TV {code}"
        self.live = code.startswith("LIVE")
        self.sid = not self.live
        return True
    def load_auth_state(self, data): self.auth.screen_id = data["screenId"]
    def linked(self): return True
    async def connect(self):
        if not self.live or self.closed:
            return False
        self.connects += 1; self.sid = True; self.drop = asyncio.Event()
        if self.now and self.in_bind:
            await self.listener.now_playing_changed(now_playing(*self.now))
        return True
    async def get_now_playing(self):
        if self.now and not self.in_bind:
            async def reply():
                await asyncio.sleep(0.02)
                await self.listener.now_playing_changed(now_playing(*self.now))
            asyncio.create_task(reply())
    async def subscribe(self): await self.drop.wait()
    def connected(self): return self.sid and not self.closed
    async def close(self):
        self.closed = True
        if self.drop: self.drop.set()
    async def set_playback_speed(self, s):
        await self.listener.playback_speed_changed(types.SimpleNamespace(playback_speed=s))


main.LoungeApi = FakeLounge
main.RECONNECT_S = 0.3  # each idle reconnect attempt pushes a (debounced) change: leave gaps for drain()
GRACE = 0.8
main.LIVE_ACTIVITY_END_GRACE_S = GRACE
main.live_push.DEBOUNCE_S = 0.1  # well inside the grace, as 0.5 s is inside 60 s
T = [2_000_000.0]
main.live.clock = lambda: T[0]

passed = []


def check(name, cond, info=""):
    assert cond, f"{name}: {info}"
    passed.append(name)


def H(k): return {"Authorization": f"Bearer {TOK[k]}"}
def stored(): return json.loads((DATA / "installs.json").read_text())
def take():
    out = relay_hits[:]; relay_hits.clear(); return out
def events(b): return sorted((x["event"], x["device_token"]) for x in b)
def nothing_but_updates():  # a playing/paused change is pushed as an update; no start or end
    return [x for x in take() if x["event"] != "update"] == []
def wait_for(cond, s=5):
    end = time.time() + s
    while time.time() < end:
        if cond(): return True
        time.sleep(0.02)
    return False
def logs(text): return [r for r in Logs.records if text in r.getMessage()]


I1, I2, I3 = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
              "33333333-3333-4333-8333-333333333333")
PTS1, PTS2, PTS3 = "a1" * 40, "b2" * 40, "c3" * 40
ACT = iter(f"{i:02x}" * 40 for i in range(16, 255))

with TestClient(main.app) as c:
    def run(fn, *a): return c.portal.call(fn, *a)
    def drain(): run(main.live.drain)
    def nowp(d, vid, state): run(d.now_playing_changed, now_playing(vid, state))
    def state(d, s): run(d.playback_state_changed, PlaybackStateEvent({"currentTime": "1", "duration": "60", "state": str(s.value)}))
    def put(iid, body, who="A"): return c.put(f"/installs/{iid}", json=body, headers=H(who))
    def show(iid, tv, who="A"):
        """The app reports a new activity on `tv` for this install; returns its token."""
        tok = next(ACT); put(iid, {"activity": {"token": tok, "device_id": tv}}, who); drain(); take()
        return tok
    def ends(): return [x for x in take() if x["event"] == "end"]
    def past_grace(): time.sleep(GRACE + 0.3); drain()

    for who, code in (("A", "LR"), ("A", "BED"), ("B", "LR")):
        assert c.post("/pair", json={"code": code}, headers=H(who)).status_code == 200
    LR, BED, BLR = main.devices[(A, "scr-LR")], main.devices[(A, "scr-BED")], main.devices[(B, "scr-LR")]
    put(I1, {"environment": "sandbox", "push_to_start_token": PTS1})
    put(I2, {"environment": "production", "push_to_start_token": PTS2})
    put(I3, {"environment": "sandbox", "push_to_start_token": PTS3}, who="B")
    nowp(LR, "v1", State.Playing); drain()
    check("setup: playing starts activities", events(take()) == [("start", PTS1), ("start", PTS2)])
    a1, a2 = show(I1, "scr-LR"), show(I2, "scr-BED")
    a3 = show(I3, "scr-LR", "B")  # B's own pairing of the same TV

    # ---------- the video ended (YouTube's state 0, pyytlounge's Buffering)
    Logs.records[:] = []
    T[0] += 10; state(LR, State.Buffering); drain()
    check("ended: nothing sent at once", nothing_but_updates())
    check("ended: scheduled, logged at INFO", len(logs("stopped playing (ended)")) == 1
          and logs("stopped playing")[0].levelno == logging.INFO, [r.getMessage() for r in Logs.records])
    check("ended: an end arrives after the grace", wait_for(lambda: any(x["event"] == "end" for x in relay_hits), 3))
    drain(); b = take()
    check("ended: one end, to the activity showing that TV only", [(x["event"], x["device_token"]) for x in b] == [("end", a1)], b)
    e = b[0]
    check("end payload: dismissal_date now and the final content-state",
          e["dismissal_date"] == int(T[0]) and e["content_state"] == main.activity_state(LR)
          and set(e["content_state"]) == {"deviceId", "deviceName", "speed", "captions", "playing", "connected"}
          and e["environment"] == "sandbox" and e["priority"] == 10, e)
    check("ended: the activity is cleared, others kept", stored()[I1]["activity"] is None
          and stored()[I2]["activity"]["token"] == a2 and stored()[I3]["activity"]["token"] == a3, stored())
    m = logs("didn't play again")
    check("ended: logged at INFO", len(m) == 1 and m[0].levelno == logging.INFO and "ended 1 Live Activity" in m[0].getMessage(),
          [r.getMessage() for r in m])

    # ---------- the next start push-starts again (same video, within START_GAP_S of the last start)
    T[0] += 5; state(LR, State.Playing); drain(); b = take()
    check("a later Playing (same video) push-starts a fresh activity, not held back by the 2 min start gap",
          events(b) == [("start", PTS1)], b)  # I2 (showing another TV) still is
    a1 = show(I1, "scr-LR")

    # ---------- Stopped, and nowPlaying with no video
    T[0] += 5; state(LR, State.Stopped)
    check("Stopped: ends after the grace", wait_for(lambda: any(x["event"] == "end" for x in relay_hits), 3)); drain()
    b = [x for x in take() if x["event"] != "update"]  # the update showing it paused comes first
    check("Stopped: the end", [(x["event"], x["device_token"]) for x in b] == [("end", a1)] and stored()[I1]["activity"] is None, b)
    nowp(LR, "v2", State.Playing); drain(); take(); a1 = show(I1, "scr-LR")
    Logs.records[:] = []
    nowp(LR, None, None)  # what pyytlounge makes of a nowPlaying with nothing in it
    check("nowPlaying with no video: scheduled", len(logs("stopped playing (no video)")) == 1, [r.getMessage() for r in Logs.records])
    check("nowPlaying with no video: ends", wait_for(lambda: any(x["event"] == "end" for x in relay_hits), 3)); drain()
    check("no video: the end", [(x["event"], x["device_token"]) for x in take()] == [("end", a1)])
    nowp(LR, "v3", State.Playing); drain(); take(); a1 = show(I1, "scr-LR")
    Logs.records[:] = []
    nowp(LR, None, State.Paused)  # no video, whatever the state
    check("no video while 'Paused' still counts", len(logs("stopped playing (no video)")) == 1)
    check("no video while 'Paused': ends", wait_for(lambda: any(x["event"] == "end" for x in relay_hits), 3)); drain(); take()
    nowp(LR, "v3", State.Playing); drain(); take(); a1 = show(I1, "scr-LR")

    # ---------- Paused is not stopping
    Logs.records[:] = []
    state(LR, State.Paused); past_grace()
    check("Paused: nothing scheduled, nothing ended", nothing_but_updates() and logs("stopped playing") == []
          and stored()[I1]["activity"]["token"] == a1)
    nowp(LR, "v3", State.Paused); past_grace()
    check("nowPlaying Paused on a video: nothing ended", nothing_but_updates() and stored()[I1]["activity"]["token"] == a1)

    # ---------- playing again within the grace cancels
    for again, label in ((lambda: state(LR, State.Playing), "Playing"), (lambda: state(LR, State.Starting), "Starting"),
                         (lambda: nowp(LR, "v3", State.Playing), "nowPlaying Playing, same video")):
        Logs.records[:] = []
        state(LR, State.Buffering); time.sleep(GRACE / 3); again(); past_grace()
        check(f"{label} within the grace: no end", ends() == [] and stored()[I1]["activity"]["token"] == a1)
        m = logs("within the grace period")
        check(f"{label}: cancel logged at INFO", len(m) == 1 and m[0].levelno == logging.INFO, [r.getMessage() for r in Logs.records])
    state(LR, State.Stopped); time.sleep(GRACE / 3); T[0] += 1; nowp(LR, "v4", State.Playing); past_grace(); b = take()
    check("a new video within the grace: no end, only updates", b and {(x["event"], x["device_token"]) for x in b} == {("update", a1)}, b)
    state(LR, State.Buffering); time.sleep(GRACE / 3); nowp(LR, "v5", State.Starting); past_grace()
    check("a new video Starting within the grace: no end", ends() == [] and stored()[I1]["activity"]["token"] == a1)

    # ---------- an ended video that's then Paused still ends; repeated stops keep one deadline
    Logs.records[:] = []
    state(LR, State.Buffering); time.sleep(GRACE / 2); state(LR, State.Paused); state(LR, State.Stopped)
    nowp(LR, None, None)
    t0 = time.time()
    check("ended then Paused: still ends", wait_for(lambda: any(x["event"] == "end" for x in relay_hits), 3)); drain(); b = [x for x in take() if x["event"] != "update"]
    check("one pending end per TV: one end, at the first deadline", [(x["event"], x["device_token"]) for x in b] == [("end", a1)]
          and time.time() - t0 < GRACE * 0.9 and len(logs("stopped playing")) == 1, (b, time.time() - t0))
    nowp(LR, "v6", State.Playing); drain(); take(); a1 = show(I1, "scr-LR")

    # ---------- per person: B's pairing of the same TV is its own
    state(BLR, State.Buffering)
    check("B's TV stopping ends B's activity only", wait_for(lambda: any(x["event"] == "end" for x in relay_hits), 3)); drain(); b = [x for x in take() if x["event"] != "update"]
    check("  (just I3's)", [(x["event"], x["device_token"]) for x in b] == [("end", a3)]
          and stored()[I1]["activity"]["token"] == a1, b)

    Logs.records[:] = []
    state(BLR, State.Playing); drain(); take()  # a start for I3, whose activity hasn't been reported (yet)
    state(BLR, State.Stopped); past_grace()
    check("no activity showing the TV: nothing scheduled", logs("stopped playing") == [] and take() == []
          and BLR._end_task is None)

    # ---------- removing a TV with an end pending: one end (the removal's)
    state(BED, State.Buffering); time.sleep(GRACE / 3)
    r = c.delete("/devices/scr-BED", headers=H("A")); past_grace(); b = [x for x in take() if x["event"] != "update"]
    check("removal during the grace: a single end", r.status_code == 200
          and [(x["event"], x["device_token"]) for x in b] == [("end", a2)], b)

    # ---------- a live session: YouTube closed (disconnected), the TV doesn't come back
    assert c.post("/pair", json={"code": "LIVEDEN"}, headers=H("A")).status_code == 200
    DEN = main.devices[(A, "scr-LIVEDEN")]
    check("live TV connects", wait_for(lambda: DEN.api.connects == 1))
    T[0] += 200; nowp(DEN, "x1", State.Playing); drain(); take()
    d1 = show(I1, "scr-LIVEDEN")
    Logs.records[:] = []
    DEN.api.live = False; DEN.api.sid = False
    run(DEN.disconnected, types.SimpleNamespace()); run(DEN.api.drop.set)
    time.sleep(0.2); drain(); b = take()
    check("disconnected: the connected-false update first, no end yet",
          [(x["event"], x["content_state"]["connected"]) for x in b] == [("update", False)], b)
    check("disconnected: scheduled once (the session ending after it keeps the deadline)",
          len(logs("stopped playing (disconnected)")) == 1 and len(logs("stopped playing")) == 1,
          [r.getMessage() for r in logs("stopped playing")])
    check("disconnected: ends after the grace", wait_for(lambda: any(x["event"] == "end" for x in relay_hits), 3)); drain(); b = [x for x in take() if x["event"] != "update"]
    check("disconnected: the end carries connected false", [(x["event"], x["device_token"]) for x in b] == [("end", d1)]
          and b[0]["content_state"]["connected"] is False and stored()[I1]["activity"] is None, b)
    DEN.api.live, DEN.api.now = True, ("x1", State.Playing)
    check("the TV comes back", wait_for(lambda: DEN.api.connects == 2, 3)); time.sleep(0.2); drain(); b = take()
    check("coming back playing push-starts again", ("start", PTS1) in events(b), b)
    d1 = show(I1, "scr-LIVEDEN")

    # ---------- the session ends but the TV comes back playing (bind response or nowPlaying answer)
    for in_bind, label in ((False, "nowPlaying answer"), (True, "bind response")):
        Logs.records[:] = []
        DEN.api.in_bind, n = in_bind, DEN.api.connects
        run(DEN.api.drop.set)
        check(f"session cycle ({label}): reconnects", wait_for(lambda: DEN.api.connects == n + 1, 3))
        past_grace()
        check(f"session cycle ({label}): scheduled, then cancelled by the reconnect's Playing",
              len(logs("stopped playing (session ended)")) == 1 and len(logs("within the grace period")) == 1,
              [r.getMessage() for r in Logs.records if "lounge event" not in r.getMessage()])
        check(f"session cycle ({label}): no end", ends() == [] and stored()[I1]["activity"]["token"] == d1)
    DEN.api.in_bind = False

    # ---------- the session ends and the TV comes back Paused: still showing a video, not stopping
    Logs.records[:] = []
    DEN.api.now, n = ("x1", State.Paused), DEN.api.connects
    run(DEN.api.drop.set)
    check("session cycle (Paused): reconnects", wait_for(lambda: DEN.api.connects == n + 1, 3)); past_grace()
    check("session cycle (Paused): cancelled, no end", ends() == [] and stored()[I1]["activity"]["token"] == d1
          and len(logs("reported Paused within the grace period")) == 1, [r.getMessage() for r in Logs.records])

    # ---------- our own forced resubscribe while playing: nothing scheduled, nothing ended
    DEN.api.now, n = ("x1", State.Playing), DEN.api.connects
    state(DEN, State.Playing); Logs.records[:] = []
    DEN._resubscribed_at = float("-inf")
    async def resubscribe(): DEN._resubscribe()
    run(resubscribe)
    check("forced resubscribe: a fresh session", wait_for(lambda: DEN.api.connects == n + 1, 3)); past_grace()
    check("forced resubscribe: nothing scheduled, no end", logs("stopped playing") == [] and ends() == []
          and stored()[I1]["activity"]["token"] == d1, [r.getMessage() for r in Logs.records])

    # ---------- the session ends and the TV doesn't come back
    Logs.records[:] = []
    DEN.api.live = False; DEN.api.sid = False; run(DEN.api.drop.set)
    check("session ended, no reconnect: ends after the grace", wait_for(lambda: any(x["event"] == "end" for x in relay_hits), 3))
    drain(); b = [x for x in take() if x["event"] == "end"]
    check("  the end", [x["device_token"] for x in b] == [d1] and len(logs("stopped playing (session ended)")) == 1, b)

    errs = [r for r in Logs.records if r.levelno >= logging.ERROR]
    check("nothing logged an error", errs == [], [r.getMessage() for r in errs])

# ---------- pushes off: nothing scheduled
main.devices.clear()
relay = main.live.relay
main.live.relay = None
with TestClient(main.app) as c:
    LR = main.devices[(A, "scr-LR")]
    Logs.records[:] = []
    c.portal.call(LR.playback_state_changed, PlaybackStateEvent({"currentTime": "1", "duration": "1", "state": "0"}))
    check("pushes off: no pending end", LR._end_task is None and logs("stopped playing") == [])
main.live.relay = relay

print(f"\n{len(passed)} checks passed")
