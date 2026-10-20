"""End-to-end API checks for Canopy sign-in and per-person TVs.

python -I tests/test_api.py [repo path, default: this repo]

A fake account service runs on 127.0.0.1 (real HTTP through aiohttp), pyytlounge's YtLoungeApi is
replaced with a fake, and DATA_DIR is a temp dir. Nothing touches a real TV or account service.
"""
import asyncio, json, os, socket, sys, tempfile, threading
from pathlib import Path

from aiohttp import web

REPO = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent)  # default: this repo
sys.path.insert(0, REPO)

# ---------- fake account service
TOK = {k: k * 43 for k in "ABUNVC"}
PERSON_A = {"id": "aaaaaaaa-0000-4000-8000-000000000001", "emailVerified": True, "firstName": "Ana",
            "lastName": "Lima", "shortName": "Ana L", "photoUrl": None, "findable": False}
PERSON_B = dict(PERSON_A, id="bbbbbbbb-0000-4000-8000-000000000002", firstName="Bo")
ANSWERS = {
    TOK["A"]: {"person": PERSON_A},
    TOK["B"]: {"person": PERSON_B},
    TOK["C"]: {"person": dict(PERSON_A, id="cccccccc-0000-4000-8000-000000000003")},
    TOK["U"]: {"person": None, "unverified": True},
    TOK["N"]: {"person": None},
    TOK["V"]: {"person": dict(PERSON_A, id="dddddddd-0000-4000-8000-000000000004", emailVerified=False)},
}
SITE_KEY = "cnp_test_key"
acct = {"mode": "up", "hits": []}


async def session(req):
    acct["hits"].append(dict(req.headers))
    if acct["mode"] == "500":
        return web.Response(status=500, text="boom")
    if req.headers.get("Authorization") != f"Bearer {SITE_KEY}":
        return web.json_response({"error": "unknown or revoked site key"}, status=401)
    return web.json_response(ANSWERS.get(req.headers.get("X-Canopy-Session"), {"person": None}))


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


PORT, DEAD_PORT = free_port(), free_port()
ready = threading.Event()


def serve():
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    a = web.Application(); a.router.add_get("/api/session", session)
    runner = web.AppRunner(a); loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", PORT).start())
    ready.set(); loop.run_forever()


threading.Thread(target=serve, daemon=True).start()
ready.wait(5)

# ---------- data dir with one owned entry and one legacy (ownerless) entry
DATA = Path(tempfile.mkdtemp())
(DATA / "devices.json").write_text(json.dumps([
    {"name": "Old TV", "auth": {"version": 1, "screenId": "scr-legacy"}},
    {"owner": PERSON_A["id"], "name": "Saved TV", "auth": {"version": 1, "screenId": "scr-saved"}},
]))
os.environ.update(DATA_DIR=str(DATA), CANOPY_ACCOUNT_URL=f"http://127.0.0.1:{PORT}/", CANOPY_ACCOUNT_KEY=SITE_KEY)

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# ---------- fake Lounge API
class FakeAuth:
    def __init__(self): self.screen_id = None
    def serialize(self): return {"version": 1, "screenId": self.screen_id}


class FakeLounge:
    all = []

    def __init__(self, name, listener=None, remote_key=None):
        self.listener, self.auth, self.screen_name, self.closed = listener, FakeAuth(), None, False
        FakeLounge.all.append(self)
    async def __aenter__(self): return self
    async def pair(self, code):
        if code == "bad": return False
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
            await self.listener.playback_speed_changed(type("E", (), {"playback_speed": s}))
        asyncio.create_task(reply())


main.LoungeApi = FakeLounge
T = [1000.0]
main.canopy.clock = lambda: T[0]

passed = []


def check(name, cond, info=""):
    assert cond, f"{name}: {info}"
    passed.append(name)


def H(k): return {"Authorization": f"Bearer {TOK[k]}"}


def err(r, status, reason=None):
    body = r.json()
    ok = r.status_code == status and isinstance(body.get("detail"), str) and body.get("reason") == reason
    return ok, (r.status_code, body)


with TestClient(main.app) as c:
    # --- legacy entries dropped and file rewritten
    saved = json.loads((DATA / "devices.json").read_text())
    check("legacy entry dropped from devices.json", saved == [
        {"owner": PERSON_A["id"], "name": "Saved TV", "auth": {"version": 1, "screenId": "scr-saved"}}], saved)
    check("legacy device not loaded", set(main.devices) == {(PERSON_A["id"], "scr-saved")}, main.devices)

    # --- 401 / 403
    r = c.get("/devices"); check("401 without a token", *err(r, 401, "signed_out"))
    check("401 has WWW-Authenticate", r.headers.get("www-authenticate") == "Bearer")
    n = len(acct["hits"])
    for bad in ("Bearer short", f"Bearer {TOK['A']}x", f"Bearer {'!' * 43}", f"Basic {TOK['A']}", TOK["A"]):
        check(f"401 malformed token {bad[:12]!r}", *err(c.get("/devices", headers={"Authorization": bad}), 401, "signed_out"))
    check("malformed tokens never reach the account service", len(acct["hits"]) == n)
    check("401 when person is null", *err(c.get("/devices", headers=H("N")), 401, "signed_out"))
    check("403 when unverified", *err(c.get("/devices", headers=H("U")), 403, "email_unverified"))
    check("403 when person has emailVerified false", *err(c.get("/devices", headers=H("V")), 403, "email_unverified"))
    for method, path in (("post", "/pair"), ("delete", "/devices/scr-saved"), ("post", "/devices/scr-saved/speed/1.5"),
                         ("post", "/devices/scr-saved/captions/on")):
        kw = {"json": {"code": "X"}} if path == "/pair" else {}
        check(f"401 on {method.upper()} {path}", *err(getattr(c, method)(path, **kw), 401, "signed_out"))

    # --- what the account service is sent
    n = len(acct["hits"])
    r = c.get("/devices", headers=H("A"))
    check("A sees only the saved TV", r.status_code == 200 and [d["id"] for d in r.json()] == ["scr-saved"], r.text)
    check("device shape unchanged", set(r.json()[0]) == {"id", "name", "connected", "video_id", "speed", "captions"})
    h = acct["hits"][-1]
    check("account service called once", len(acct["hits"]) == n + 1)
    check("site key sent as bearer", h.get("Authorization") == f"Bearer {SITE_KEY}", h)
    check("token sent as X-Canopy-Session", h.get("X-Canopy-Session") == TOK["A"], h)
    check("no X-Canopy-Site-Host", "X-Canopy-Site-Host" not in h, h)
    c.get("/devices", headers=H("A"))
    check("answer cached within 60 s", len(acct["hits"]) == n + 1)
    check("cache keyed by sha256, not the token", TOK["A"] not in main.canopy.cache and all(len(k) == 64 for k in main.canopy.cache))
    T[0] += 61; c.get("/devices", headers=H("A"))
    check("asked again after 60 s", len(acct["hits"]) == n + 2)

    # --- unreachable
    main.canopy.base = f"http://127.0.0.1:{DEAD_PORT}"
    check("503 unreachable with no cache", *err(c.get("/devices", headers=H("C")), 503, "account_unreachable"))
    T[0] += 120
    r = c.get("/devices", headers=H("A"))
    check("stale cached answer used when unreachable", r.status_code == 200 and len(r.json()) == 1, r.text)
    T[0] += 15 * 60
    check("503 once the cached answer is over 15 min old", *err(c.get("/devices", headers=H("A")), 503, "account_unreachable"))
    main.canopy.base = f"http://127.0.0.1:{PORT}"
    acct["mode"] = "500"
    check("503 on account service 500 with no cache", *err(c.get("/devices", headers=H("C")), 503, "account_unreachable"))
    acct["mode"] = "up"
    r = c.get("/devices", headers=H("A")); check("recovers when the account service is back", r.status_code == 200)
    T[0] += 120; acct["mode"] = "500"
    r = c.get("/devices", headers=H("A")); check("stale cached answer used on a 500", r.status_code == 200, r.text)
    acct["mode"] = "up"
    main.canopy.key = "cnp_wrong"
    check("503 when the site key is rejected", *err(c.get("/devices", headers=H("C")), 503, "account_unreachable"))
    main.canopy.key = SITE_KEY
    T[0] += 61  # let A's cached answer go stale

    # --- isolation between people
    for method, path in (("delete", "/devices/scr-saved"), ("post", "/devices/scr-saved/speed/1.5"),
                         ("post", "/devices/scr-saved/captions/on"), ("delete", "/devices/nope"),
                         ("post", "/devices/nope/speed/1.5")):
        r = getattr(c, method)(path, headers=H("B"))
        check(f"B gets 404 on {method.upper()} {path}", r.status_code == 404 and r.json() == {"detail": "unknown device"}, r.text)
    check("B sees no TVs", c.get("/devices", headers=H("B")).json() == [])
    r = c.post("/devices/scr-saved/speed/1.5", headers=H("A"))
    check("A can control own TV", r.status_code == 200 and r.json()["speed"] == 1.5, r.text)
    check("A unknown id 404", c.post("/devices/nope/captions/on", headers=H("A")).status_code == 404)

    # --- same TV paired by two people
    ra = c.post("/pair", json={"code": "TV1"}, headers=H("A"))
    rb = c.post("/pair", json={"code": "T V1"}, headers=H("B"))  # spaces stripped
    check("A pairs TV1", ra.status_code == 200 and ra.json()["id"] == "scr-TV1", ra.text)
    check("B pairs the same TV1", rb.status_code == 200 and rb.json()["id"] == "scr-TV1", rb.text)
    da, db = main.devices[(PERSON_A["id"], "scr-TV1")], main.devices[(PERSON_B["id"], "scr-TV1")]
    check("separate Lounge sessions", da is not db and da.api is not db.api)
    check("B sees only TV1", [d["id"] for d in c.get("/devices", headers=H("B")).json()] == ["scr-TV1"])
    owners = sorted(e["owner"] for e in json.loads((DATA / "devices.json").read_text()) if e["auth"]["screenId"] == "scr-TV1")
    check("devices.json records both owners", owners == sorted([PERSON_A["id"], PERSON_B["id"]]), owners)
    r = c.delete("/devices/scr-TV1", headers=H("A"))
    check("A removes TV1", r.status_code == 200 and r.json() == {"removed": "scr-TV1"}, r.text)
    check("B still has TV1", [d["id"] for d in c.get("/devices", headers=H("B")).json()] == ["scr-TV1"] and not db.api.closed)
    check("A's TV1 session closed", da.api.closed)

    # --- re-pair replaces, pairing failure
    old = main.devices[(PERSON_B["id"], "scr-TV1")]
    r = c.post("/pair", json={"code": "TV1"}, headers=H("B"))
    check("re-pair replaces the owner's entry", r.status_code == 200 and len(c.get("/devices", headers=H("B")).json()) == 1
          and old.api.closed and main.devices[(PERSON_B["id"], "scr-TV1")] is not old)
    r = c.post("/pair", json={"code": "bad"}, headers=H("B"))
    check("pairing failed is 400", r.status_code == 400 and r.json() == {"detail": "pairing failed"}, r.text)

    # --- 10-TV limit (A has scr-saved already)
    for i in range(2, 11):
        assert c.post("/pair", json={"code": f"TV{i}"}, headers=H("A")).status_code == 200
    check("A has 10 TVs", len(c.get("/devices", headers=H("A")).json()) == 10)
    before = len(FakeLounge.all)
    r = c.post("/pair", json={"code": "TV11"}, headers=H("A"))
    check("11th TV is 409 too_many_devices", *err(r, 409, "too_many_devices"))
    check("refused pairing's session closed", FakeLounge.all[before].closed)
    check("still 10 TVs", len(c.get("/devices", headers=H("A")).json()) == 10)
    check("re-pairing at the limit works", c.post("/pair", json={"code": "TV5"}, headers=H("A")).status_code == 200)
    check("limit is per person", c.post("/pair", json={"code": "TV11"}, headers=H("B")).status_code == 200)

    # --- web UI gone, docs stay
    check("GET / is gone", c.get("/").status_code == 404 and c.get("/", headers=H("A")).status_code == 404)
    check("/docs served", c.get("/docs").status_code == 200)
    check("/openapi.json served", c.get("/openapi.json").status_code == 200)
    check("index.html deleted", not (Path(REPO) / "index.html").exists())

    # --- existing errors keep detail
    r = c.post("/devices/scr-saved/speed/3", headers=H("A"))
    check("bad speed still 400 with detail", r.status_code == 400 and "detail" in r.json() and "reason" not in r.json(), r.text)

# --- startup refused without a key
main.devices.clear()
saved_canopy, main.canopy = main.canopy, None
try:
    with TestClient(main.app):
        pass
    check("refuses to start without CANOPY_ACCOUNT_KEY", False, "started")
except RuntimeError as ex:
    check("refuses to start without CANOPY_ACCOUNT_KEY", "CANOPY_ACCOUNT_KEY" in str(ex), ex)
main.canopy = saved_canopy

print(f"\n{len(passed)} checks passed")
