"""Per-person settings (GET/PATCH /me/settings, auto_captions) and video title/channel in DeviceInfo.

python -I tests/test_settings.py [repo path, default: this repo]

A fake account service and a fake oEmbed server run on 127.0.0.1, main.LoungeApi is
replaced with a fake, the app's title fetcher is a fake, and DATA_DIR is a temp dir. Nothing
touches YouTube, a real TV or the account service.
"""
import asyncio, json, logging, os, socket, sys, tempfile, threading, time, types
from pathlib import Path

from aiohttp import web

REPO = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent)  # default: this repo
sys.path.insert(0, REPO)

# ---------- fake account service and fake oEmbed
TOK = {k: k * 43 for k in "ABN"}
A, B = "aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002"
ANSWERS = {TOK["A"]: {"person": {"id": A, "emailVerified": True}},
           TOK["B"]: {"person": {"id": B, "emailVerified": True}}}
SITE_KEY = "cnp_test_key"
oe_hits = []


async def session(req):
    if req.headers.get("Authorization") != f"Bearer {SITE_KEY}":
        return web.json_response({"error": "bad key"}, status=401)
    return web.json_response(ANSWERS.get(req.headers.get("X-Canopy-Session"), {"person": None}))


async def oembed_route(req):
    oe_hits.append(dict(req.query))
    vid = req.query.get("url", "").rsplit("=", 1)[-1]
    if vid == "privateVid1":
        return web.Response(status=401, text="Unauthorized")
    if vid == "slowVideo01":
        await asyncio.sleep(3)
    if vid == "notJson0001":
        return web.Response(text="<html>")
    return web.json_response({"title": f"Title {vid}", "author_name": f"Chan {vid}", "type": "video"})


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


PORT = free_port()
ready = threading.Event()


def serve():
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    a = web.Application(); a.router.add_get("/api/session", session); a.router.add_get("/oembed", oembed_route)
    runner = web.AppRunner(a); loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", PORT).start())
    ready.set(); loop.run_forever()


threading.Thread(target=serve, daemon=True).start()
ready.wait(5)

DATA = Path(tempfile.mkdtemp())
for k in ("ISPONSORBLOCKTV_CONFIG", "SKIP_HIDDEN_PERSON_IDS", "CANOPY_APNS_KEY"):
    os.environ.pop(k, None)
os.environ.update(DATA_DIR=str(DATA), CANOPY_ACCOUNT_URL=f"http://127.0.0.1:{PORT}", CANOPY_ACCOUNT_KEY=SITE_KEY)

import main  # noqa: E402
import oembed  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pyytlounge.models import State  # noqa: E402


class FakeAuth:
    def __init__(self): self.screen_id = None
    def serialize(self): return {"version": 1, "screenId": self.screen_id}


class FakeLounge:
    """Like the real TV: confirms speed and caption changes with events; ignores languageCode=""."""
    def __init__(self, name, listener=None, remote_key=None):
        self.listener, self.auth, self.screen_name, self.closed, self.sent = listener, FakeAuth(), None, False, []
    async def __aenter__(self): return self
    async def pair(self, code):
        self.auth.screen_id, self.screen_name = f"scr-{code}", f"TV {code}"
        return True
    def load_auth_state(self, data): self.auth.screen_id = data["screenId"]
    def linked(self): return True
    async def connect(self): return False  # _loop just idles
    def connected(self): return not self.closed
    async def close(self): self.closed = True
    async def set_playback_speed(self, s):
        async def reply():
            await asyncio.sleep(0.05)
            await self.listener.playback_speed_changed(types.SimpleNamespace(playback_speed=s))
        asyncio.create_task(reply())
    async def set_closed_captions(self, lang, vid):
        return await self._command("setSubtitlesTrack", {"languageCode": lang, "videoId": vid})
    async def _command(self, cmd, params):
        assert cmd == "setSubtitlesTrack"
        self.sent.append(params)
        lang = params.get("languageCode")
        if lang != "":
            async def reply():
                await asyncio.sleep(0.05)
                await self.listener.subtitles_track_changed(types.SimpleNamespace(
                    video_id=params["videoId"], language_code=lang or "", kind=None))
            asyncio.create_task(reply())
        return True


main.LoungeApi = FakeLounge

# ---------- fake title fetcher: per-id answers, an optional gate to hold a fetch open
fetched, held = [], set()
FAKE = {}  # video_id -> (title, channel) or an exception to raise


async def fake_fetch(vid):
    fetched.append(vid)
    while vid in held:
        await asyncio.sleep(0.01)
    ans = FAKE.get(vid, (f"Title {vid}", f"Chan {vid}"))
    if isinstance(ans, Exception):
        raise ans
    return ans


main.titles.fetch = fake_fetch
TT = [5000.0]
main.titles.clock = lambda: TT[0]


class Logs(logging.Handler):
    records = []
    def emit(self, r): Logs.records.append(r)


logging.getLogger("oembed").addHandler(Logs())

passed = []


def check(name, cond, info=""):
    assert cond, f"{name}: {info}"
    passed.append(name)


def H(k): return {"Authorization": f"Bearer {TOK[k]}"}
def ev(**kw): return types.SimpleNamespace(**kw)
def wait_for(cond, s=3):
    end = time.time() + s
    while time.time() < end:
        if cond(): return True
        time.sleep(0.01)
    return False


def stored(): return json.loads((DATA / "settings.json").read_text()) if (DATA / "settings.json").exists() else None


V1, V2, V3, V4, V5 = "aaaaaaaaaa1", "bbbbbbbbbb2", "cccccccccc3", "dddddddddd4", "eeeeeeeeee5"

with TestClient(main.app) as c:
    def run(fn, *a): return c.portal.call(fn, *a)
    def play(d, vid): run(d.now_playing_changed, ev(video_id=vid, state=State.Playing))
    def drain(): run(main.titles.drain)
    def speed(x, who="A", tv="scr-LR"): return c.post(f"/devices/{tv}/speed/{x}", headers=H(who))
    def caps(mode, who="A", tv="scr-LR"): return c.post(f"/devices/{tv}/captions/{mode}", headers=H(who))
    def me(who="A"): return c.get("/me/settings", headers=H(who))
    def set_me(body, who="A", **kw): return c.patch("/me/settings", headers=H(who), **({"json": body} | kw))
    def tv(who="A", tv_id="scr-LR"): return next(d for d in c.get("/devices", headers=H(who)).json() if d["id"] == tv_id)

    # --- settings: auth, defaults, PATCH, 422s, isolation, storage
    r = c.get("/me/settings")
    check("GET /me/settings without a token is 401", r.status_code == 401 and r.json()["reason"] == "signed_out", r.text)
    r = c.patch("/me/settings", json={"auto_captions": False})
    check("PATCH /me/settings without a token is 401", r.status_code == 401 and r.json()["reason"] == "signed_out", r.text)
    check("PATCH with a signed-out token is 401", c.patch("/me/settings", json={"auto_captions": False},
                                                         headers=H("N")).status_code == 401)
    check("no settings.json before any change", stored() is None)
    r = me()
    check("GET default is auto_captions true", r.status_code == 200 and r.json() == {"auto_captions": True}, r.text)
    for body in ({}, {"auto_captions": None}, {"auto_captions": "false"}, {"auto_captions": 0}, {"auto_captions": 1},
                 {"other": True}, [], "x"):
        r = set_me(body)
        check(f"PATCH {body!r} is 422", r.status_code == 422, (r.status_code, r.text))
    r = c.patch("/me/settings", headers=H("A"))
    check("PATCH with no body is 422", r.status_code == 422, r.text)
    check("refused PATCHes stored nothing", stored() is None and me().json() == {"auto_captions": True})
    r = set_me({"auto_captions": True})
    check("PATCH to the default: full object, nothing stored", r.status_code == 200 and r.json() == {"auto_captions": True}
          and stored() is None, (r.text, stored()))
    r = set_me({"auto_captions": False})
    check("PATCH false returns the full object", r.status_code == 200 and r.json() == {"auto_captions": False}, r.text)
    check("GET reflects it", me().json() == {"auto_captions": False})
    check("stored per person id, only non-defaults", stored() == {A: {"auto_captions": False}}, stored())
    check("B still has the default", me("B").json() == {"auto_captions": True})
    check("no temp files left", sorted(p.name for p in DATA.iterdir() if p.name.startswith(".")) == [],
          list(DATA.iterdir()))
    r = set_me({"auto_captions": True})
    check("PATCH back to true", r.json() == {"auto_captions": True} and stored() == {}, stored())
    r = set_me({"auto_captions": False, "extra": 1})
    check("unknown fields beside auto_captions are ignored", r.status_code == 200 and r.json() == {"auto_captions": False}
          and stored() == {A: {"auto_captions": False}}, (r.text, stored()))

    # --- TVs: A has LR; A and B both have DEN
    for who, code in (("A", "LR"), ("A", "DEN"), ("B", "DEN")):
        assert c.post("/pair", json={"code": code}, headers=H(who)).status_code == 200
    LR, ADEN, BDEN = main.devices[(A, "scr-LR")], main.devices[(A, "scr-DEN")], main.devices[(B, "scr-DEN")]

    # --- title/channel: null with no video, null while fetching, filled after
    r = c.get("/devices", headers=H("A"))
    check("DeviceInfo has title and channel, null with no video",
          all(d["video_id"] is None and d["title"] is None and d["channel"] is None for d in r.json()), r.text)
    held.add(V1)
    play(LR, V1)
    check("a new video starts one fetch", wait_for(lambda: fetched == [V1]), fetched)
    t = tv()
    check("title/channel null before the fetch finishes", t["video_id"] == V1 and t["title"] is None
          and t["channel"] is None, t)
    check("still one fetch while in flight", fetched == [V1], fetched)
    held.discard(V1); drain()
    t = tv()
    check("title/channel filled after the fetch", t["title"] == f"Title {V1}" and t["channel"] == f"Chan {V1}", t)
    play(ADEN, V1); play(BDEN, V1)
    check("a cached id isn't fetched again (any TV, any person)", fetched == [V1], fetched)
    check("B sees it too", tv("B", "scr-DEN")["title"] == f"Title {V1}")
    FAKE[V2] = ("  Spaced  ", "")
    play(LR, V2); drain()
    t = tv()
    check("titles trimmed, empty channel is null", (t["title"], t["channel"]) == ("Spaced", None), t)

    # --- returned from pair, PATCH, speed and captions too
    r = c.patch("/devices/scr-LR", json={"name": "Living"}, headers=H("A"))
    check("PATCH /devices returns title/channel", r.json()["title"] == "Spaced" and "channel" in r.json(), r.text)
    r = c.post("/pair", json={"code": "LR"}, headers=H("A"))
    check("POST /pair returns title/channel (null: the new session has no video yet)",
          r.status_code == 200 and r.json()["title"] is None and r.json()["channel"] is None, r.text)
    LR = main.devices[(A, "scr-LR")]
    play(LR, V2)

    # --- failures: cached as null, logged at INFO once, retried after 10 min
    FAKE[V3] = oembed.Failed("oEmbed answered 404")
    n, Logs.records[:] = len(fetched), []
    play(LR, V3); drain()
    t = tv()
    check("failed fetch: title/channel null", t["video_id"] == V3 and t["title"] is None and t["channel"] is None, t)
    for _ in range(3):
        tv(); play(ADEN, V3)
    drain()
    check("failure cached: no refetch within 10 min", fetched[n:] == [V3], fetched[n:])
    logged = [r for r in Logs.records if V3 in r.getMessage()]
    check("failure logged once, at INFO", len(logged) == 1 and logged[0].levelno == logging.INFO
          and "404" in logged[0].getMessage(), [r.getMessage() for r in logged])
    FAKE[V3] = ("Back", "Chan")
    TT[0] += oembed.FAIL_S + 1
    tv(); drain()  # GET /devices wants the id again once the failure has expired
    check("refetched after the failure expires", fetched[n:] == [V3, V3], fetched[n:])
    check("and filled", tv()["title"] == "Back")
    FAKE[V4] = asyncio.TimeoutError()
    play(LR, V4); drain()
    check("timeout: null", tv()["title"] is None)
    check("timeout logged at INFO", any(V4 in r.getMessage() and r.levelno == logging.INFO for r in Logs.records))
    FAKE[V5] = (None, None)
    play(LR, V5); drain()
    check("an answer with neither field counts as a failure", main.titles.cache[V5][1] is not None)

    # --- no fetch for an id that isn't one
    n = len(fetched)
    for bad in ("short", "aaaaaaaaaaaa", "aaaaaaaaa a", "aaaaaaaaa/a", "../../etc/p", "aaaaaaaaaa\n"):
        play(LR, bad); tv()
    drain()
    check("no fetch for ids that aren't 11 [A-Za-z0-9_-]", fetched[n:] == [], fetched[n:])
    check("bad id: title/channel null", tv()["title"] is None)

    # --- speed with auto_captions false (A): only the speed
    check("A's auto_captions is off", me().json() == {"auto_captions": False})
    play(LR, V1)
    LR.api.sent.clear()
    r = speed(1.5)
    check("off: speed changes", r.status_code == 200 and r.json()["speed"] == 1.5, r.text)
    check("off: no caption command", LR.api.sent == [], LR.api.sent)
    check("off: want_captions unchanged (none yet)", LR.want_captions is None)
    check("off: captions stay off", r.json()["captions"] is False)
    play(LR, V2)
    check("off: nothing re-applied on a new video", LR.api.sent == [], LR.api.sent)
    r = caps("on")
    check("off: manual captions on still works", r.status_code == 200 and r.json()["captions"] is True
          and LR.api.sent == [{"languageCode": "en", "videoId": V2}] and LR.want_captions is True, (r.text, LR.api.sent))
    r = speed(1.0)
    check("off: speed 1 sends nothing, want_captions stays true", r.status_code == 200 and r.json()["speed"] == 1.0
          and len(LR.api.sent) == 1 and LR.want_captions is True and r.json()["captions"] is True, (r.text, LR.api.sent))
    # Below, the TV's answer to each re-apply has to land before the next caption command: any
    # caption event wakes a waiting set_captions.
    LR._captions_changed.clear(); play(LR, V3)
    check("off: the manual choice is re-applied per video", LR.api.sent[-1] == {"languageCode": "en", "videoId": V3},
          LR.api.sent)
    check("  and the TV confirms it", wait_for(LR._captions_changed.is_set) and LR.captions is True)
    r = caps("off")
    check("off: manual captions off still works (videoId only)", r.status_code == 200 and r.json()["captions"] is False
          and LR.api.sent[-1] == {"videoId": V3} and LR.want_captions is False, (r.text, LR.api.sent))
    n = len(LR.api.sent)
    speed(2.0)
    check("off: speed 2 sends nothing, want_captions stays false", len(LR.api.sent) == n and LR.want_captions is False)
    LR._captions_changed.clear(); play(LR, V4)
    check("off: re-applied off on the next video", LR.api.sent[-1] == {"videoId": V4}, LR.api.sent)
    check("  and the TV confirms it", wait_for(LR._captions_changed.is_set) and LR.captions is False)

    # --- speed with auto_captions true (B, the default): as before
    play(BDEN, V1)
    BDEN.api.sent.clear(); ADEN.api.sent.clear()
    r = speed(1.5, "B", "scr-DEN")
    check("on: speed above 1 turns captions on", r.status_code == 200 and r.json()["captions"] is True
          and BDEN.api.sent == [{"languageCode": "en", "videoId": V1}] and BDEN.want_captions is True, (r.text, BDEN.api.sent))
    r = speed(1.0, "B", "scr-DEN")
    check("on: speed 1 turns them off, sent as videoId only", r.json()["captions"] is False
          and BDEN.api.sent[-1] == {"videoId": V1} and BDEN.want_captions is False, BDEN.api.sent)
    r = speed(1.25, "B", "scr-DEN")
    play(BDEN, V2)
    check("on: re-applied to the next video", BDEN.api.sent[-1] == {"languageCode": "en", "videoId": V2}, BDEN.api.sent)
    check("the same TV paired by A (off) got nothing from B's requests", ADEN.api.sent == [], ADEN.api.sent)
    play(ADEN, V2)
    r = speed(1.5, "A", "scr-DEN")
    check("A's speed on the shared TV sends no captions", r.status_code == 200 and ADEN.api.sent == []
          and ADEN.want_captions is None, ADEN.api.sent)
    BDEN.want_captions = None
    BDEN.video_id = None
    speed(1.5, "B", "scr-DEN")
    check("on, no video: want_captions set for the next video", BDEN.want_captions is True)

    # --- A turns it back on: same as before
    set_me({"auto_captions": True})
    LR.api.sent.clear()
    r = speed(1.75)
    check("A on again: captions follow the slider", r.json()["captions"] is True
          and LR.api.sent == [{"languageCode": "en", "videoId": V4}] and LR.want_captions is True, (r.text, LR.api.sent))
    set_me({"auto_captions": False})

# ---------- restart: settings reloaded
main.devices.clear()
(DATA / "settings.json").write_text(json.dumps({A: {"auto_captions": False}, B: {"auto_captions": True},
                                                "": {"auto_captions": False}, "x": "junk",
                                                "y": {"auto_captions": "no", "other": 1}}))
with TestClient(main.app) as c:
    check("restart: stored setting reloaded", c.get("/me/settings", headers=H("A")).json() == {"auto_captions": False})
    check("restart: default values and junk dropped on load", main.settings == {A: {"auto_captions": False}}, main.settings)
    check("restart: B default", c.get("/me/settings", headers=H("B")).json() == {"auto_captions": True})
main.devices.clear()
(DATA / "settings.json").write_text("{not json")
with TestClient(main.app) as c:
    check("unreadable settings.json: defaults, still starts", c.get("/me/settings", headers=H("A")).json() == {
        "auto_captions": True})

# ---------- the real fetcher against a local fake oEmbed server (never YouTube)
async def real_fetcher():
    oembed.URL = f"http://127.0.0.1:{PORT}/oembed"
    oembed.TIMEOUT_S = 0.5
    t = oembed.Titles()
    clock = [0.0]; t.clock = lambda: clock[0]
    Logs.records[:] = []
    for vid in ("goodVideo01", "privateVid1", "slowVideo01", "notJson0001"):
        t.want(vid)
    t.want("goodVideo01")  # in flight: not twice
    await t.drain()
    check("real fetch: title and author_name", t.get("goodVideo01") == ("Title goodVideo01", "Chan goodVideo01"),
          t.get("goodVideo01"))
    q = next(h for h in oe_hits if h["url"].endswith("goodVideo01"))
    check("real fetch: the oEmbed query", q == {"url": "https://www.youtube.com/watch?v=goodVideo01", "format": "json"}, q)
    check("real fetch: one request per id", sum(h["url"].endswith("goodVideo01") for h in oe_hits) == 1)
    for vid in ("privateVid1", "slowVideo01", "notJson0001"):
        check(f"real fetch: {vid} cached as null for 10 min", t.get(vid) == (None, None)
              and t.cache[vid][1] == oembed.FAIL_S, t.cache.get(vid))
    msgs = [r.getMessage() for r in Logs.records]
    check("real fetch: one INFO line per failure", len(msgs) == 3 and all(r.levelno == logging.INFO for r in Logs.records)
          and any("401" in m for m in msgs), msgs)
    t.want("privateVid1"); await t.drain()
    check("real fetch: failure not retried early", sum(h["url"].endswith("privateVid1") for h in oe_hits) == 1)
    oembed.CACHE_MAX = 3
    for vid in ("lru00000001", "lru00000002", "lru00000003"):
        t.want(vid)
        await t.drain()
    check("LRU keeps at most CACHE_MAX ids", list(t.cache) == ["lru00000001", "lru00000002", "lru00000003"], list(t.cache))
    t.get("lru00000001")  # touched: most recently used
    t.want("lru00000004"); await t.drain()
    check("LRU evicts the least recently used", list(t.cache) == ["lru00000003", "lru00000001", "lru00000004"],
          list(t.cache))
    t.want("goodVideo01")
    await t.close()
    check("close cancels fetches in flight and closes the session", not t._pending and t._session is None)


asyncio.run(real_fetcher())
t = oembed.Titles(); t.want("noLoop00001")
check("want() without a running loop does nothing", not t._pending and not t.cache)

print(f"\n{len(passed)} checks passed")
