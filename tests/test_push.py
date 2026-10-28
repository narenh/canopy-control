"""Live Activity push checks: /installs, start/update/end pushes, debounce, relay results.

python -I tests/test_push.py [repo path, default: this repo]   (also runs itself with `nokey`)

A fake account service and a fake canopy-apns relay run on 127.0.0.1 (real HTTP through aiohttp),
pyytlounge's YtLoungeApi is replaced with a fake, and DATA_DIR is a temp dir. Nothing touches a
real TV, the account service, the relay or production.
"""
import asyncio, json, logging, os, socket, subprocess, sys, tempfile, threading, time, types
from pathlib import Path

from aiohttp import web

REPO = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent)  # default: this repo
NOKEY = sys.argv[2:] == ["nokey"]
sys.path.insert(0, REPO)

# ---------- fake account service + relay
TOK = {k: k * 43 for k in "AB"}
PERSON_A = {"id": "aaaaaaaa-0000-4000-8000-000000000001", "emailVerified": True}
PERSON_B = {"id": "bbbbbbbb-0000-4000-8000-000000000002", "emailVerified": True}
ANSWERS = {TOK["A"]: {"person": PERSON_A}, TOK["B"]: {"person": PERSON_B}}
SITE_KEY, APNS_KEY = "cnp_test_key", "relay_test_key"
relay = {"hits": [], "answers": {}, "status": None}  # answers: device_token -> (result, reason)


async def session(req):
    if req.headers.get("Authorization") != f"Bearer {SITE_KEY}":
        return web.json_response({"error": "bad key"}, status=401)
    return web.json_response(ANSWERS.get(req.headers.get("X-Canopy-Session"), {"person": None}))


async def liveactivity(req):
    body = await req.json()
    relay["hits"].append({"auth": req.headers.get("Authorization"), "body": body})
    if relay["status"]:
        return web.json_response({"detail": "nope"}, status=relay["status"])
    result, reason = relay["answers"].get(body.get("device_token"), ("delivered", None))
    return web.json_response({"result": result, "reason": reason, "apns_id": "x" if result == "delivered" else None})


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
os.environ.update(DATA_DIR=str(DATA), CANOPY_ACCOUNT_URL=f"http://127.0.0.1:{ACCT_PORT}", CANOPY_ACCOUNT_KEY=SITE_KEY,
                  CANOPY_APNS_URL=f"http://127.0.0.1:{RELAY_PORT}/")
os.environ.pop("CANOPY_CONTROL_BUNDLE_ID", None)
if NOKEY:
    os.environ.pop("CANOPY_APNS_KEY", None)
else:
    os.environ["CANOPY_APNS_KEY"] = APNS_KEY


class Logs(logging.Handler):
    records = []
    def emit(self, r): Logs.records.append(r)


import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pyytlounge.models import State  # noqa: E402

logging.getLogger().addHandler(Logs())


# ---------- fake Lounge API
class FakeAuth:
    def __init__(self): self.screen_id = None
    def serialize(self): return {"version": 1, "screenId": self.screen_id}


class FakeLounge:
    """Codes starting with LIVE connect through _loop (subscribe blocks until dropped); others
    never connect through _loop but report connected, like test_api's fake."""

    def __init__(self, name, listener=None, remote_key=None):
        self.listener, self.auth, self.screen_name, self.closed = listener, FakeAuth(), None, False
        self.live, self.sid, self.drop, self.connects = False, False, None, 0
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
        return True
    async def get_now_playing(self): pass
    async def subscribe(self): await self.drop.wait()
    def connected(self): return self.sid and not self.closed
    async def close(self):
        self.closed = True
        if self.drop: self.drop.set()
    async def set_playback_speed(self, s):
        async def reply():
            await asyncio.sleep(0.05)
            await self.listener.playback_speed_changed(types.SimpleNamespace(playback_speed=s))
        asyncio.create_task(reply())
    async def set_closed_captions(self, lang, vid):
        return await self._command("setSubtitlesTrack", {"languageCode": lang, "videoId": vid})
    async def _command(self, cmd, params):
        async def reply():
            await asyncio.sleep(0.05)
            await self.listener.subtitles_track_changed(types.SimpleNamespace(
                video_id=params["videoId"], language_code=params.get("languageCode", ""), kind=None))
        asyncio.create_task(reply())
        return True


main.LoungeApi = FakeLounge
main.RECONNECT_S = 1.0
T = [1_000_000.0]
main.live.clock = lambda: T[0]

passed = []


def check(name, cond, info=""):
    assert cond, f"{name}: {info}"
    passed.append(name)


def H(k): return {"Authorization": f"Bearer {TOK[k]}"}


I1, I2, I3 = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
              "33333333-3333-4333-8333-333333333333")
PTS1, PTS2, PTS3 = "a1" * 40, "b2" * 40, "c3" * 40
ACT1, ACT1B, ACT2, ACT3, ACT4 = "d4" * 40, "e5" * 40, "f6" * 40, "07" * 40, "18" * 40
A, B = PERSON_A["id"], PERSON_B["id"]
BUNDLE = "com.canopysf.CanopyControl"
STATE_KEYS = {"deviceId", "deviceName", "speed", "captions", "playing", "connected"}


def ev(**kw): return types.SimpleNamespace(**kw)


def stored(): return json.loads((DATA / "installs.json").read_text()) if (DATA / "installs.json").exists() else {}


def take():
    out = relay["hits"][:]; relay["hits"].clear()
    return out


def bodies(): return [h["body"] for h in take()]


def wait_for(cond, s=5):
    end = time.time() + s
    while time.time() < end:
        if cond(): return True
        time.sleep(0.02)
    return False


def run_nokey():
    with TestClient(main.app) as c:
        off = [r for r in Logs.records if "CANOPY_APNS_KEY is not set" in r.getMessage()]
        check("push off: one INFO at startup", len(off) == 1 and off[0].levelno == logging.INFO, off)
        check("push off: no relay client", main.live.relay is None)
        assert c.post("/pair", json={"code": "LR"}, headers=H("A")).status_code == 200
        d = main.devices[(A, "scr-LR")]
        r = c.put(f"/installs/{I1}", json={"environment": "sandbox", "push_to_start_token": PTS1,
                                           "activity": {"token": ACT1, "device_id": "scr-LR"}}, headers=H("A"))
        check("push off: PUT still stores", r.status_code == 200 and stored()[I1]["activity"]["token"] == ACT1, r.text)
        c.portal.call(d.now_playing_changed, ev(video_id="v1", state=State.Playing))
        c.portal.call(d.playback_speed_changed, ev(playback_speed=1.5))
        time.sleep(0.8); c.portal.call(main.live.drain)
        check("push off: nothing sent", relay["hits"] == [], relay["hits"])
        c.delete("/devices/scr-LR", headers=H("A"))
        check("push off: TV removal still clears the activity", stored()[I1]["activity"] is None)
        check("push off: nothing sent on removal", relay["hits"] == [])
    print(f"nokey: {len(passed)} checks passed")


if NOKEY:
    run_nokey()
    sys.exit(0)

with TestClient(main.app) as c:
    def run(fn, *a): return c.portal.call(fn, *a)
    def drain(): c.portal.call(main.live.drain)
    def play(d, vid): run(d.now_playing_changed, ev(video_id=vid, state=State.Playing))
    def pause(d): run(d.playback_state_changed, ev(state=State.Paused))
    def resume(d): run(d.playback_state_changed, ev(state=State.Playing))
    def put(iid, body, who="A"): return c.put(f"/installs/{iid}", json=body, headers=H(who))

    on = [r for r in Logs.records if "Live Activity pushes for" in r.getMessage()]
    check("push on: INFO at startup names the bundle and relay", len(on) == 1 and BUNDLE in on[0].getMessage())
    for who, code in (("A", "LR"), ("A", "BED"), ("B", "BR")):
        assert c.post("/pair", json={"code": code}, headers=H(who)).status_code == 200
    LR, BED = main.devices[(A, "scr-LR")], main.devices[(A, "scr-BED")]

    # ---------- PUT / DELETE validation, ownership, null-clears
    check("PUT needs sign-in", c.put(f"/installs/{I1}", json={}).status_code == 401)
    check("DELETE needs sign-in", c.delete(f"/installs/{I1}").status_code == 401)
    bad = [("not-a-uuid", {}, 400), (I1, {"environment": "staging"}, 400),
           (I1, {"push_to_start_token": "xyz"}, 400), (I1, {"push_to_start_token": "a" * 201}, 400),
           (I1, {"push_to_start_token": ""}, 400), (I1, {"activity": {"token": "zz", "device_id": "scr-LR"}}, 400),
           (I1, {"activity": {"token": ACT1, "device_id": "scr-nope"}}, 404),
           (I1, {"activity": {"token": ACT1, "device_id": "scr-BR"}}, 404),  # B's TV
           (I1, {"activity": {"token": ACT1}}, 422), (I1, {"environment": 5}, 422)]
    for iid, body, status in bad:
        r = put(iid, body)
        check(f"PUT {iid[:10]} {body} is {status}", r.status_code == status, r.text)
        if status in (400, 404):
            check(f"  with a sentence detail", isinstance(r.json().get("detail"), str), r.text)
    check("unknown device detail", put(I1, {"activity": {"token": ACT1, "device_id": "scr-BR"}}).json() == {"detail": "unknown device"})
    check("a refused PUT stores nothing", stored() == {}, stored())
    check("PUT pts up to 200 hex chars is ok", put(I1, {"push_to_start_token": "a" * 200}).json() == {"ok": True})
    r = put(I1.upper(), {"environment": "sandbox", "push_to_start_token": PTS1.upper()})
    check("PUT ok", r.status_code == 200 and r.json() == {"ok": True}, r.text)
    check("stored lowercased, owner A", stored() == {I1: {"owner": A, "environment": "sandbox", "push_to_start_token": PTS1,
                                                          "activity": None}}, stored())
    put(I1, {}); check("absent fields unchanged", stored()[I1]["push_to_start_token"] == PTS1 and stored()[I1]["environment"] == "sandbox")
    put(I1, {"push_to_start_token": None})
    check("null clears push_to_start_token only", stored()[I1]["push_to_start_token"] is None and stored()[I1]["environment"] == "sandbox")
    put(I1, {"environment": None}); check("null clears environment", stored()[I1]["environment"] is None)
    r = put(I1, {"environment": "production"}, who="B")
    check("another person's PUT replaces the install", r.status_code == 200 and stored()[I1] == {
        "owner": B, "environment": "production", "push_to_start_token": None, "activity": None}, stored())
    r = c.delete(f"/installs/{I1}", headers=H("A"))
    check("DELETE someone else's install is 404", r.status_code == 404 and r.json() == {"detail": "unknown install"} and I1 in stored())
    r = c.delete(f"/installs/{I1}", headers=H("B"))
    check("DELETE own install", r.status_code == 200 and r.json() == {"ok": True} and I1 not in stored(), r.text)
    check("DELETE unknown install is 404", c.delete(f"/installs/{I1}", headers=H("B")).status_code == 404)
    check("DELETE bad id is 400", c.delete("/installs/nope", headers=H("B")).status_code == 400)
    check("no pushes without an activity or a TV event", take() == [])

    put(I1, {"environment": "sandbox", "push_to_start_token": PTS1})
    put(I2, {"environment": "production", "push_to_start_token": PTS2})
    put(I3, {"environment": "sandbox", "push_to_start_token": PTS3}, who="B")

    # ---------- start on a new video, exact payload
    play(LR, "v1"); drain()
    hits = take()
    check("start goes to both of A's installs, not B's", sorted(h["body"]["device_token"] for h in hits) == [PTS1, PTS2], hits)
    check("relay gets the key as bearer", all(h["auth"] == f"Bearer {APNS_KEY}" for h in hits))
    st = {"deviceId": "scr-LR", "deviceName": "TV LR", "speed": 1.0, "captions": False, "playing": True, "connected": True}
    h1 = next(h["body"] for h in hits if h["body"]["device_token"] == PTS1)
    check("start payload exact", h1 == {
        "device_token": PTS1, "environment": "sandbox", "bundle_id": BUNDLE, "event": "start", "content_state": st,
        "attributes_type": "SpeedAttributes", "attributes": {}, "alert": {"title": "TV LR", "body": "Now playing"},
        "relevance_score": 100, "timestamp": int(T[0]), "priority": 10}, h1)
    check("content-state has exactly the Swift keys, no error", set(h1["content_state"]) == STATE_KEYS)
    check("each install's own environment", next(h["body"] for h in hits if h["body"]["device_token"] == PTS2)["environment"] == "production")

    T[0] += 30; pause(LR); T[0] += 30; resume(LR); play(LR, "v1"); drain()
    check("same video playing again: nothing", take() == [])
    play(LR, "v2"); drain()
    check("new video within 2 min of a start: rate-limited", take() == [])
    T[0] += 61; play(LR, "v3"); drain()
    check("after 2 min: started again", sorted(b["device_token"] for b in bodies()) == [PTS1, PTS2])

    # ---------- the app reports its activity -> immediate update
    r = put(I1, {"activity": {"token": ACT1.upper(), "device_id": "scr-LR"}}); drain()
    check("PUT activity ok", r.json() == {"ok": True} and stored()[I1]["activity"] == {"token": ACT1, "device_id": "scr-LR", "since": T[0]})
    b = bodies()
    check("PUT activity pushes the TV's state now", b == [{"device_token": ACT1, "environment": "sandbox", "bundle_id": BUNDLE,
                                                          "event": "update", "content_state": st, "timestamp": int(T[0]),
                                                          "priority": 10}], b)
    T[0] += 10; play(LR, "v4"); drain(); b = bodies()
    check("activity shows this TV: update, not start (I2 still rate-limited)",
          [(x["event"], x["device_token"]) for x in b] == [("update", ACT1)], b)
    T[0] += 120; play(LR, "v5"); drain(); b = bodies()
    check("update for I1, start for I2", sorted((x["event"], x["device_token"]) for x in b) == [("start", PTS2), ("update", ACT1)], b)
    play(BED, "w1"); drain(); b = bodies()
    check("activity shows another TV: start", sorted((x["event"], x["device_token"]) for x in b) == [("start", PTS1), ("start", PTS2)], b)
    check("start carries the other TV", all(x["content_state"]["deviceId"] == "scr-BED" and x["alert"]["title"] == "TV BED" for x in b), b)
    since = stored()[I1]["activity"]["since"]
    T[0] += 100; put(I1, {"activity": {"token": ACT1, "device_id": "scr-LR"}}); drain(); take()
    check("same activity token keeps its since", stored()[I1]["activity"]["since"] == since)
    T[0] = since + 8 * 3600 + 1; play(LR, "v6"); drain(); b = bodies()
    check("activity over 8 h old: start", sorted((x["event"], x["device_token"]) for x in b) == [("start", PTS1), ("start", PTS2)], b)
    put(I1, {"activity": {"token": ACT1B, "device_id": "scr-LR"}}); drain()
    check("new activity token: new since", stored()[I1]["activity"]["since"] == T[0] and [x["device_token"] for x in bodies()] == [ACT1B])

    # ---------- debounced updates, no duplicates
    run(LR.playback_speed_changed, ev(playback_speed=1.25))
    run(LR.playback_speed_changed, ev(playback_speed=1.5))
    run(LR.subtitles_track_changed, ev(video_id="v6", language_code="en", kind=None))
    t0 = time.time(); drain(); b = bodies()
    check("burst coalesced into one update", len(b) == 1 and b[0]["event"] == "update" and b[0]["device_token"] == ACT1B, b)
    check("debounced ~0.5 s", 0.4 < time.time() - t0 < 1.5, time.time() - t0)
    check("update carries the final state", b[0]["content_state"] == dict(st, speed=1.5, captions=True), b)
    check("update priority 10", b[0]["priority"] == 10)
    run(LR.playback_speed_changed, ev(playback_speed=1.5)); drain()
    check("unchanged state: nothing sent", take() == [])
    r = c.post("/devices/scr-LR/speed/1.75", headers=H("A")); drain(); b = bodies()
    check("speed via the API: one update", r.status_code == 200 and len(b) == 1 and b[0]["content_state"]["speed"] == 1.75, b)
    r = c.post("/devices/scr-LR/captions/off", headers=H("A")); drain(); b = bodies()
    check("captions via the API: one update", r.status_code == 200 and len(b) == 1 and b[0]["content_state"]["captions"] is False, b)
    check("timestamps keep increasing per token", b[0]["timestamp"] > int(T[0]) - 1)

    # ---------- a live session: connected false on drop, name change, first Playing after reconnect
    assert c.post("/pair", json={"code": "LIVEDEN"}, headers=H("A")).status_code == 200
    DEN = main.devices[(A, "scr-LIVEDEN")]
    check("live TV connects", wait_for(lambda: DEN.api.connects == 1))
    drain(); take()
    put(I2, {"activity": {"token": ACT2, "device_id": "scr-LIVEDEN"}}); drain()
    check("I2 activity on the den", [x["content_state"]["connected"] for x in bodies()] == [True])
    T[0] += 130; play(DEN, "x1"); drain(); b = bodies()
    check("den starts: I2 update, I1 start", sorted((x["event"], x["device_token"]) for x in b) == [("start", PTS1), ("update", ACT2)], b)
    T[0] += 10; pause(DEN); resume(DEN); drain(); b = bodies()
    check("pause/resume: counts as starting again: the showing activity gets an update, and no "
          "second start within the start limit", sorted((x["event"], x["device_token"]) for x in b) == [("update", ACT2)], b)
    DEN.api.live = False; DEN.api.sid = False
    run(DEN.disconnected, ev()); run(DEN.api.drop.set)
    time.sleep(0.2); drain(); b = bodies()
    check("dropped session pushes connected false", [x["content_state"]["connected"] for x in b] == [False], b)
    DEN.api.screen_name = "Den TV"; DEN.api.live = True
    check("live TV reconnects", wait_for(lambda: DEN.api.connects == 2, 3))
    time.sleep(0.1); drain(); b = bodies()
    check("reconnect pushes connected true and the new name",
          [(x["content_state"]["connected"], x["content_state"]["deviceName"]) for x in b] == [(True, "Den TV")], b)
    T[0] += 130; resume(DEN); drain(); b = bodies()
    check("first Playing after reconnect (same video) counts as starting",
          sorted((x["event"], x["device_token"]) for x in b) == [("start", PTS1), ("update", ACT2)], b)
    check("start alert uses the new name", next(x for x in b if x["event"] == "start")["alert"]["title"] == "Den TV")

    # ---------- resuming after a pause (past the start limit): start where none shows, update where one does
    T[0] += 130; pause(DEN); T[0] += 29 * 60; resume(DEN); drain(); b = bodies()
    check("resume after a long pause: I1 start, I2 update", sorted((x["event"], x["device_token"]) for x in b) == [("start", PTS1), ("update", ACT2)], b)

    # ---------- 30 minutes without Playing (the video ended; not a pause)
    def ended(d): run(d.playback_state_changed, ev(state=State.Buffering))
    T[0] += 130; ended(DEN); T[0] += 29 * 60; resume(DEN); drain()
    check("Playing again 29 min after it ended: nothing", take() == [])
    ended(DEN); T[0] += 30 * 60; resume(DEN); drain(); b = bodies()
    check("Playing again 30 min after it ended: counts as starting", sorted((x["event"], x["device_token"]) for x in b) == [("start", PTS1), ("update", ACT2)], b)

    # ---------- end on TV removal
    put(I3, {"activity": {"token": ACT4, "device_id": "scr-BR"}}, who="B"); drain(); take()
    r = c.delete("/devices/scr-LR", headers=H("A")); drain(); b = bodies()
    check("removing a TV ends its activity", r.status_code == 200 and len(b) == 1 and b[0]["event"] == "end"
          and b[0]["device_token"] == ACT1B and b[0]["dismissal_date"] == int(T[0]) and b[0]["content_state"]["deviceId"] == "scr-LR", b)
    check("and forgets it", stored()[I1]["activity"] is None and stored()[I2]["activity"]["device_id"] == "scr-LIVEDEN"
          and stored()[I3]["activity"]["token"] == ACT4)

    # ---------- relay results
    relay["answers"][PTS1] = ("failed", "BadDeviceToken")
    T[0] += 130; play(BED, "w2"); drain(); take()
    check("BadDeviceToken keeps the push-to-start token", stored()[I1]["push_to_start_token"] == PTS1)
    check("BadDeviceToken logged with a hint", any("BadDeviceToken" in r.getMessage() and "environment" in r.getMessage() for r in Logs.records))
    relay["answers"][PTS1] = ("unregistered", None)
    T[0] += 130; play(BED, "w3"); drain(); take()
    check("unregistered clears the push-to-start token", stored()[I1]["push_to_start_token"] is None and stored()[I2]["push_to_start_token"] == PTS2)
    T[0] += 130; play(BED, "w4"); drain(); b = bodies()
    check("no start without a push-to-start token", [x["device_token"] for x in b] == [PTS2], b)
    relay["answers"][ACT2] = ("unregistered", None)
    run(DEN.playback_speed_changed, ev(playback_speed=2.0)); drain(); take()
    check("unregistered clears the activity", stored()[I2]["activity"] is None and stored()[I2]["push_to_start_token"] == PTS2)
    relay["answers"][ACT3] = ("failed", "BadDeviceToken")
    put(I2, {"activity": {"token": ACT3, "device_id": "scr-BED"}}); drain(); take()
    check("BadDeviceToken keeps the activity", stored()[I2]["activity"]["token"] == ACT3)
    run(BED.playback_speed_changed, ev(playback_speed=1.0)); drain()
    check("after a failed push the same state is tried again", [x["device_token"] for x in bodies()] == [ACT3])
    del relay["answers"][ACT3]
    for status in (401, 403, 422, 429, 503):
        relay["status"] = status
        run(BED.playback_speed_changed, ev(playback_speed=1.25 if status % 2 else 1.5)); drain()
        check(f"relay {status}: logged, nothing cleared, nothing crashed",
              len(take()) == 1 and stored()[I2]["activity"]["token"] == ACT3)
    relay["status"] = None

    # ---------- renaming a TV (I2's activity shows the bedroom TV)
    def patch(dev, body, who="A"): return c.patch(f"/devices/{dev}", json=body, headers=H(who))
    def saved_entry(dev, owner=A):
        return next(e for e in json.loads((DATA / "devices.json").read_text())
                    if e["owner"] == owner and e["auth"]["screenId"] == dev)
    take()
    check("PATCH needs sign-in", c.patch("/devices/scr-BED", json={"name": "x"}).status_code == 401)
    r = patch("scr-BED", {"name": "  Bedroom  "}); drain(); b = bodies()
    check("rename trims and returns the TV", r.status_code == 200 and r.json()["name"] == "Bedroom"
          and set(r.json()) == {"id", "name", "connected", "video_id", "title", "channel", "speed", "captions", "playing"}, r.text)
    check("GET /devices shows the new name", next(x for x in c.get("/devices", headers=H("A")).json() if x["id"] == "scr-BED")["name"] == "Bedroom")
    check("devices.json keeps both names", saved_entry("scr-BED")["name"] == "TV BED" and saved_entry("scr-BED")["custom_name"] == "Bedroom")
    check("rename pushes to the activity showing that TV",
          [(x["event"], x["device_token"], x["content_state"]["deviceName"]) for x in b] == [("update", ACT3, "Bedroom")], b)
    patch("scr-BED", {"name": "Bedroom"}); drain(); check("same name again: nothing sent", take() == [])
    check("40 characters ok", patch("scr-BED", {"name": "x" * 40}).json()["name"] == "x" * 40)
    for body, status in (({"name": "x" * 41}, 400), ({"name": "   "}, 400), ({}, 422), ({"name": 5}, 422)):
        r = patch("scr-BED", body)
        check(f"rename {str(body)[:20]} is {status}", r.status_code == status, r.text)
        if status == 400:
            check("  with a sentence detail", "40 characters" in r.json()["detail"], r.text)
    check("refused renames leave the name", main.devices[(A, "scr-BED")].display_name == "x" * 40)
    for who, dev in (("B", "scr-BED"), ("A", "scr-BR"), ("A", "scr-nope")):
        r = patch(dev, {"name": "Mine"}, who)
        check(f"{who} renaming {dev} is 404", r.status_code == 404 and r.json() == {"detail": "unknown device"}, r.text)
    check("B's TV keeps its name", main.devices[(B, "scr-BR")].display_name == "TV BR")
    r = patch("scr-BED", {"name": None})
    check("null resets to the TV's own name", r.json()["name"] == "TV BED" and "custom_name" not in saved_entry("scr-BED"), r.text)
    patch("scr-BED", {"name": "Bedroom"}); r = patch("scr-BED", {"name": ""})
    check("empty string resets too", r.json()["name"] == "TV BED" and main.devices[(A, "scr-BED")].custom_name is None, r.text)
    patch("scr-BED", {"name": "Bedroom"}); drain(); take()
    r = c.post("/pair", json={"code": "BED"}, headers=H("A"))
    check("re-pairing keeps the custom name", r.json()["name"] == "Bedroom" and saved_entry("scr-BED")["custom_name"] == "Bedroom", r.text)
    BED = main.devices[(A, "scr-BED")]
    # the den: rename, then the TV reconnects reporting a new name of its own
    patch("scr-LIVEDEN", {"name": "Den"})
    DEN.api.live = False; DEN.api.sid = False; run(DEN.api.drop.set)
    wait_for(lambda: not DEN.api.connected()); time.sleep(0.1)
    DEN.api.screen_name = "Den TV 2"; DEN.api.live = True
    check("den reconnects", wait_for(lambda: DEN.api.connects == 3, 3))
    time.sleep(0.1)
    check("custom name survives a reconnect", DEN.info()["name"] == "Den" and DEN.name == "Den TV 2"
          and saved_entry("scr-LIVEDEN")["name"] == "Den TV 2" and saved_entry("scr-LIVEDEN")["custom_name"] == "Den")
    drain(); take()
    errs =[r for r in Logs.records if r.name == "live-push" and r.levelno >= logging.ERROR and "task failed" in r.getMessage()]
    check("no push task ever crashed", errs == [], [r.getMessage() for r in errs])
    before = stored()

# ---------- installs survive a restart
main.devices.clear()
with TestClient(main.app) as c:
    check("installs reloaded at startup", main.live.installs == before, main.live.installs)
    names = {k[1]: d.display_name for k, d in main.devices.items() if k[0] == A}
    check("custom names reloaded at startup", names.get("scr-BED") == "Bedroom" and names.get("scr-LIVEDEN") == "Den", names)

# ---------- push off without CANOPY_APNS_KEY (a fresh process: config is read at import)
p = subprocess.run([sys.executable, "-I", __file__, REPO, "nokey"], capture_output=True, text=True, timeout=60)
check("push-off run passed", p.returncode == 0 and "checks passed" in p.stdout, p.stdout[-2000:] + p.stderr[-3000:])
print(p.stdout.strip())

print(f"\n{len(passed)} checks passed")
