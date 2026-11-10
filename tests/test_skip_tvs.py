"""Sponsor Block only TVs: /skip-tvs, converting through /pair, the rendered config, skip_tvs.json
and scripts/import_skip_tvs.py.

python -I tests/test_skip_tvs.py [repo path, default: this repo]   (also runs itself with `off`)

Same fakes as test_skip.py: an account service on 127.0.0.1, pyytlounge's YtLoungeApi replaced, temp
DATA_DIR and config path. Screen ids are obviously fake ("scr-...").
"""
import asyncio, json, os, socket, subprocess, sys, tempfile, threading
from pathlib import Path

from aiohttp import web

REPO = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent)  # default: this repo
OFF = sys.argv[2:] == ["off"]
sys.path.insert(0, REPO)

TOK = {k: k * 43 for k in "ABH"}
A, B, HID = ("aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002",
             "hhhhhhhh-0000-4000-8000-000000000003")
ANSWERS = {TOK["A"]: {"person": {"id": A, "emailVerified": True}},
           TOK["B"]: {"person": {"id": B, "emailVerified": True}},
           TOK["H"]: {"person": {"id": HID, "emailVerified": True}}}
SITE_KEY = "cnp_test_key"


async def session(req):
    if req.headers.get("Authorization") != f"Bearer {SITE_KEY}":
        return web.json_response({"error": "bad key"}, status=401)
    return web.json_response(ANSWERS.get(req.headers.get("X-Canopy-Session"), {"person": None}))


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


PORT = free_port()
ready = threading.Event()


def serve():
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    a = web.Application(); a.router.add_get("/api/session", session)
    runner = web.AppRunner(a); loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", PORT).start())
    ready.set(); loop.run_forever()


threading.Thread(target=serve, daemon=True).start()
ready.wait(5)

DATA = Path(tempfile.mkdtemp())
CONF = Path(tempfile.mkdtemp()) / "isbtv" / "config.json"
SKIPFILE = DATA / "skip_tvs.json"
(DATA / "devices.json").write_text(json.dumps([
    {"owner": A, "name": "TV LIV", "custom_name": "Living", "auth": {"version": 1, "screenId": "scr-LIV"}},
]))
for k in ("SKIP_CATEGORIES", "SKIP_ADS", "MUTE_ADS", "AUTO_PLAY", "SKIP_MINIMUM_LENGTH", "SKIP_JOIN_NAME",
          "SKIP_COUNT_TRACKING", "ISPONSORBLOCKTV_CONFIG", "SKIP_HIDDEN_PERSON_IDS", "CANOPY_APNS_KEY"):
    os.environ.pop(k, None)
os.environ.update(DATA_DIR=str(DATA), CANOPY_ACCOUNT_URL=f"http://127.0.0.1:{PORT}", CANOPY_ACCOUNT_KEY=SITE_KEY)
if not OFF:
    os.environ.update(ISPONSORBLOCKTV_CONFIG=str(CONF), SKIP_HIDDEN_PERSON_IDS=HID)

import isbtv  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class FakeAuth:
    def __init__(self): self.screen_id = None
    def serialize(self): return {"version": 1, "screenId": self.screen_id}


class FakeLounge:
    all = []
    def __init__(self, name, listener=None, remote_key=None):
        self.listener, self.auth, self._name, self.closed = listener, FakeAuth(), None, False
        self.connects, self.paired_with = 0, None
        FakeLounge.all.append(self)
    @property
    def screen_name(self): return self._name
    async def __aenter__(self): return self
    async def pair(self, code):
        self.paired_with = code
        if code.startswith("BAD"):
            return False
        if code.startswith("ERR"):
            raise ValueError("no screen")
        self.auth.screen_id, self._name = f"scr-{code}", f"TV {code}"
        return True
    def load_auth_state(self, data): self.auth.screen_id = data["screenId"]
    def linked(self): return True
    async def connect(self):
        self.connects += 1
        return False
    def connected(self): return not self.closed
    async def close(self): self.closed = True


main.LoungeApi = FakeLounge
passed = []


def check(name, cond, info=""):
    assert cond, f"{name}: {info}"
    passed.append(name)


def H(k): return {"Authorization": f"Bearer {TOK[k]}"}


def stored(): return json.loads(SKIPFILE.read_text())


def run_off():
    with TestClient(main.app) as c:
        check("off: ISBTV is None", main.ISBTV is None)
        for method, url, body in (("get", "/skip-tvs", None), ("post", "/skip-tvs", {"code": "X"}),
                                  ("patch", "/skip-tvs/scr-X", {"name": "N"}), ("delete", "/skip-tvs/scr-X", None)):
            r = getattr(c, method)(url, headers=H("A"), **({"json": body} if body else {}))
            check(f"off: {method} {url} is 403 skip_not_allowed", r.status_code == 403
                  and r.json().get("reason") == "skip_not_allowed", r.text)
        check("off: nothing was stored or rendered", not SKIPFILE.exists() and not CONF.parent.exists())
        check("off: no Lounge API was made", len(FakeLounge.all) == 1)  # only the saved Device
    print(f"off: {len(passed)} checks passed")


if OFF:
    run_off()
    sys.exit(0)


def conf_ids(): return [d["screen_id"] for d in json.loads(CONF.read_text())["devices"]]


def ino(): st = CONF.stat(); return (st.st_ino, st.st_mtime_ns)


with TestClient(main.app) as c:
    def add(code, who="A"): return c.post("/skip-tvs", json={"code": code}, headers=H(who))
    def lst(who="A"): return c.get("/skip-tvs", headers=H(who))

    # --- startup: missing file is fine; A's paired TV has skip off, so the config is empty
    check("no skip_tvs.json yet: empty list", lst().status_code == 200 and lst().json() == [])
    check("config rendered at startup, no devices", CONF.exists() and conf_ids() == [])

    N0 = len(FakeLounge.all)  # the Device loaded from devices.json
    # --- gating
    for method, url, body in (("get", "/skip-tvs", None), ("post", "/skip-tvs", {"code": "HX"}),
                              ("patch", "/skip-tvs/scr-HX", {"name": "N"}), ("delete", "/skip-tvs/scr-HX", None)):
        r = getattr(c, method)(url, headers=H("H"), **({"json": body} if body else {}))
        check(f"hidden person: {method} {url} is 403 skip_not_allowed", r.status_code == 403 and r.json() == {
            "detail": r.json()["detail"], "reason": "skip_not_allowed"}, r.text)
    check("hidden person: nothing paired or stored", len(FakeLounge.all) == N0 and not SKIPFILE.exists())
    check("needs sign-in", c.get("/skip-tvs").status_code == 401 and c.post("/skip-tvs", json={"code": "X"}).status_code == 401)

    # --- add / list
    r = add("1 2 3")
    check("add returns {id, name}", r.status_code == 200 and r.json() == {"id": "scr-123", "name": "TV 123"}, r.text)
    lounge = FakeLounge.all[-1]
    check("code spaces are stripped, like /pair", lounge.paired_with == "123")
    check("pairing leaves no session: never connected, HTTP session closed", lounge.connects == 0 and lounge.closed)
    check("no Device, no Lounge loop", (A, "scr-123") not in main.devices)
    check("list shows it", lst().json() == [{"id": "scr-123", "name": "TV 123"}])
    check("GET /devices doesn't", [x["id"] for x in c.get("/devices", headers=H("A")).json()] == ["scr-LIV"])
    check("stored as owner/screen_id/name", stored() == [{"owner": A, "screen_id": "scr-123", "name": "TV 123"}], stored())
    check("rendered into the config", conf_ids() == ["scr-123"])
    check("other people don't see it", lst("B").json() == [])
    for code in ("BAD1", "ERR1"):
        r = add(code)
        check(f"{code}: 400 pairing failed", r.status_code == 400 and r.json()["detail"] == "pairing failed", r.text)
        check(f"{code}: its API was closed", FakeLounge.all[-1].closed and FakeLounge.all[-1].connects == 0)
    check("failed pairings stored nothing", len(stored()) == 1)

    # --- re-adding returns the entry, keeping the custom name, without rewriting anything
    before = ino()
    r = c.patch("/skip-tvs/scr-123", json={"name": "  Den  "}, headers=H("A"))
    check("rename trims, returns the custom name", r.status_code == 200 and r.json() == {"id": "scr-123", "name": "Den"}, r.text)
    check("rename stored custom_name", stored()[0]["custom_name"] == "Den" and stored()[0]["name"] == "TV 123")
    check("rename does not rewrite the config", ino() == before)
    r = add("123")
    check("re-adding returns the existing entry with its custom name", r.status_code == 200
          and r.json() == {"id": "scr-123", "name": "Den"} and len(stored()) == 1, r.text)
    check("re-adding doesn't rewrite the config", ino() == before)
    for body, status in (({"name": "x" * 41}, 400), ({"name": "   "}, 400), ({}, 422)):
        r = c.patch("/skip-tvs/scr-123", json=body, headers=H("A"))
        check(f"PATCH {body} is {status}", r.status_code == status, r.text)
    check("a refused rename changed nothing", lst().json() == [{"id": "scr-123", "name": "Den"}])
    for body in ({"name": None}, {"name": ""}):
        c.patch("/skip-tvs/scr-123", json={"name": "Den"}, headers=H("A"))
        r = c.patch("/skip-tvs/scr-123", json=body, headers=H("A"))
        check(f"PATCH {body} goes back to the TV's own name", r.json() == {"id": "scr-123", "name": "TV 123"}
              and "custom_name" not in stored()[0], r.text)
    check("PATCH of someone else's / unknown TV is 404", c.patch("/skip-tvs/scr-123", json={"name": "N"}, headers=H("B")).status_code == 404
          and c.patch("/skip-tvs/scr-nope", json={"name": "N"}, headers=H("A")).status_code == 404)

    # --- already paired
    r = add("LIV")
    check("already a paired TV: 409 already_paired with the device id", r.status_code == 409
          and r.json()["reason"] == "already_paired" and r.json()["id"] == "scr-LIV" == r.json()["device_id"]
          and isinstance(r.json()["detail"], str), r.text)
    check("and nothing was stored", [e["screen_id"] for e in stored()] == ["scr-123"])
    check("B may add the TV A has paired", add("LIV", "B").status_code == 200)

    # --- shared TV: delete one entry, the TV stays for the other
    check("both in the config once", conf_ids().count("scr-LIV") == 1 and "scr-LIV" in conf_ids())
    check("re-adding keeps one entry", add("LIV", "B").status_code == 200 and len(stored()) == len({(e["owner"], e["screen_id"]) for e in stored()}))
    add("SHARED"); add("SHARED", "B")
    check("two owners, one config entry", conf_ids().count("scr-SHARED") == 1)
    r = c.delete("/skip-tvs/scr-SHARED", headers=H("A"))
    check("DELETE returns {removed}", r.status_code == 200 and r.json() == {"removed": "scr-SHARED"}, r.text)
    check("shared TV stays in the config for the other owner", "scr-SHARED" in conf_ids())
    check("and in the other owner's list", [x["id"] for x in lst("B").json()] == ["scr-LIV", "scr-SHARED"])
    c.delete("/skip-tvs/scr-SHARED", headers=H("B"))
    check("last holder removes it: gone from the config", "scr-SHARED" not in conf_ids())
    check("DELETE of an unknown / someone else's TV is 404", c.delete("/skip-tvs/scr-SHARED", headers=H("A")).status_code == 404
          and c.delete("/skip-tvs/scr-123", headers=H("B")).status_code == 404)
    c.delete("/skip-tvs/scr-LIV", headers=H("B"))

    # --- render: union with paired skip_sponsors TVs, deduped, no hidden owners
    check("paired TV with skip off is not in the config", "scr-LIV" not in conf_ids())
    c.patch("/devices/scr-LIV", json={"skip_sponsors": True}, headers=H("A"))
    check("paired skip-on TV joins the union", sorted(conf_ids()) == ["scr-123", "scr-LIV"], conf_ids())
    main.skip_tvs[(B, "scr-LIV")] = {"owner": B, "screen_id": "scr-LIV", "name": "dup"}
    main.skip_tvs[(HID, "scr-HID")] = {"owner": HID, "screen_id": "scr-HID", "name": "hid"}
    main.render_isbtv()
    check("deduped by screen id, hidden owners' entries left out", sorted(conf_ids()) == ["scr-123", "scr-LIV"], conf_ids())
    del main.skip_tvs[(B, "scr-LIV")], main.skip_tvs[(HID, "scr-HID")]
    main.render_isbtv()
    check("config names are labels, not display names", all(d["name"] == isbtv.label(d["screen_id"]) for d in json.loads(CONF.read_text())["devices"]))

    # --- convert through /pair
    c.patch("/skip-tvs/scr-123", json={"name": "Den"}, headers=H("A"))
    before = ino()
    r = c.post("/pair", json={"code": "123"}, headers=H("A"))
    check("/pair of a skip-only TV: becomes a Device", r.status_code == 200 and r.json()["id"] == "scr-123", r.text)
    check("keeps the custom name", r.json()["name"] == "Den" and r.json()["skip_sponsors"] is True, r.text)
    check("the skip-only entry is gone", lst().json() == [] and not any(e["screen_id"] == "scr-123" for e in stored()))
    check("now in GET /devices", "scr-123" in [x["id"] for x in c.get("/devices", headers=H("A")).json()])
    check("config unchanged by the conversion (no restart)", ino() == before and "scr-123" in conf_ids())
    check("saved custom name in devices.json", any(e.get("custom_name") == "Den" for e in json.loads((DATA / "devices.json").read_text())
                                                   if e["auth"]["screenId"] == "scr-123"))
    r = add("123")
    check("and now it is already_paired", r.status_code == 409 and r.json()["reason"] == "already_paired")

    # --- limit
    for i in range(10):
        r = add(f"L{i}", "B")
        check(f"B adds TV {i + 1}", r.status_code == 200, r.text)
    r = add("L10", "B")
    check("11th is 409 too_many_devices", r.status_code == 409 and r.json()["reason"] == "too_many_devices"
          and "10" in r.json()["detail"], r.text)
    check("re-adding one at the limit still works", add("L3", "B").status_code == 200)
    check("the limit is per person", add("L10", "A").status_code == 200)
    check("a refused add stored nothing", sum(e["owner"] == B for e in stored()) == 10)

    # --- a failed save rolls the add back
    real = main.write_atomic
    def boom(path, text): raise OSError("disk full")
    main.write_atomic = boom
    try:
        try:
            r = add("FAIL"); status = r.status_code
        except Exception:
            status = 500
    finally:
        main.write_atomic = real
    check("a failed save is an error and isn't kept in memory", status == 500 and (A, "scr-FAIL") not in main.skip_tvs)

# ---------- restart: reloaded from skip_tvs.json; config not rewritten
snapshot, text = dict(main.skip_tvs), CONF.read_text()
main.skip_tvs.clear(); main.devices.clear()
before = ino()
with TestClient(main.app) as c:
    check("restart: entries reloaded", main.skip_tvs == snapshot)
    check("restart: same config, not rewritten", CONF.read_text() == text and ino() == before)

# ---------- corrupt or odd skip_tvs.json never crashes startup
for label, content in (("not JSON", "{{{"), ("not a list", '{"a": 1}'), ("empty file", "")):
    SKIPFILE.write_text(content)
    main.skip_tvs.clear(); main.devices.clear()
    with TestClient(main.app) as c:
        check(f"corrupt ({label}): starts, empty list", c.get("/skip-tvs", headers=H("A")).json() == [])
        check(f"corrupt ({label}): file set aside, not destroyed",
              (DATA / "skip_tvs.json.corrupt").read_text() == content and not SKIPFILE.exists())
        r = c.post("/skip-tvs", json={"code": "NEW"}, headers=H("A"))
        check(f"corrupt ({label}): adding works again", r.status_code == 200 and len(stored()) == 1)
    SKIPFILE.unlink()
SKIPFILE.write_text(json.dumps([
    {"owner": A, "screen_id": "scr-OK", "name": "Ok", "custom_name": "Mine"},
    {"owner": A, "screen_id": "scr-OK", "name": "Dup"},
    {"owner": "", "screen_id": "scr-X"}, {"screen_id": "scr-Y"}, "junk", None, {"owner": A, "screen_id": 5},
    {"owner": B, "screen_id": "scr-NONAME"}]))
main.skip_tvs.clear(); main.devices.clear()
with TestClient(main.app) as c:
    check("bad entries skipped, good ones kept (first wins, defaults filled)", main.skip_tvs == {
        (A, "scr-OK"): {"owner": A, "screen_id": "scr-OK", "name": "Ok", "custom_name": "Mine"},
        (B, "scr-NONAME"): {"owner": B, "screen_id": "scr-NONAME", "name": "TV"}}, main.skip_tvs)

# ---------- the import script
SCRIPT = str(Path(REPO) / "scripts" / "import_skip_tvs.py")
IMP = Path(tempfile.mkdtemp())
(IMP / "devices.json").write_text(json.dumps([{"owner": A, "name": "P", "auth": {"version": 1, "screenId": "scr-PAIRED-0001"}}]))
src = IMP / "in.json"
src.write_text(json.dumps([{"name": "Bedroom", "screen_id": "scr-FAKEFAKE-0001"}, {"name": "Den", "screen_id": "scr-FAKEFAKE-0002"},
                           {"name": "Paired", "screen_id": "scr-PAIRED-0001"}, {"name": "No id"}, {"name": "Bedroom again", "screen_id": "scr-FAKEFAKE-0001"}]))


def imp(*extra):
    return subprocess.run([sys.executable, "-I", SCRIPT, str(src), "--owner", A, "--data", str(IMP), *extra],
                          capture_output=True, text=True, timeout=60)


p = imp()
check("import: runs", p.returncode == 0, p.stdout + p.stderr)
imported = json.loads((IMP / "skip_tvs.json").read_text())
check("import: merged, deduped, paired and invalid skipped", imported == [
    {"owner": A, "screen_id": "scr-FAKEFAKE-0001", "name": "Bedroom"},
    {"owner": A, "screen_id": "scr-FAKEFAKE-0002", "name": "Den"}], imported)
check("import: summary counts", "2 added" in p.stdout and "1 already there" in p.stdout and "1 already paired" in p.stdout
      and "1 skipped as invalid" in p.stdout, p.stdout)
check("import: never prints a full screen id", "scr-FAKEFAKE-0001" not in p.stdout + p.stderr and "scr-FAKE" in p.stdout
      and "scr-PAIRED-0001" not in p.stdout, p.stdout)
(IMP / "skip_tvs.json").write_text(json.dumps([dict(imported[0], custom_name="Mine")]))
first = (IMP / "skip_tvs.json").read_text()
p = imp()
second = json.loads((IMP / "skip_tvs.json").read_text())
check("import: re-run adds only the missing one, keeps a custom name", p.returncode == 0 and len(second) == 2
      and second[0]["custom_name"] == "Mine" and "1 added" in p.stdout, p.stdout)
mtime = (IMP / "skip_tvs.json").stat().st_mtime_ns
p = imp()
check("import: idempotent (nothing to add: file untouched)", p.returncode == 0 and json.loads((IMP / "skip_tvs.json").read_text()) == second
      and (IMP / "skip_tvs.json").stat().st_mtime_ns == mtime and "0 added" in p.stdout, p.stdout)
check("import: no temp files left", sorted(x.name for x in IMP.iterdir()) == ["devices.json", "in.json", "skip_tvs.json"], list(IMP.iterdir()))
(IMP / "skip_tvs.json").write_text("{{{")
p = imp()
check("import: refuses to overwrite a corrupt skip_tvs.json", p.returncode != 0 and (IMP / "skip_tvs.json").read_text() == "{{{", p.stdout + p.stderr)
# the server reads what the script wrote
(IMP / "skip_tvs.json").write_text(json.dumps(second))
main.SKIP_TVS_FILE = IMP / "skip_tvs.json"
main.load_skip_tvs()
check("import: main.py loads the script's file", set(main.skip_tvs) == {(A, "scr-FAKEFAKE-0001"), (A, "scr-FAKEFAKE-0002")})

# ---------- feature off (a fresh process: config is read at import)
p = subprocess.run([sys.executable, "-I", __file__, REPO, "off"], capture_output=True, text=True, timeout=60)
check("feature-off run passed", p.returncode == 0 and "checks passed" in p.stdout, p.stdout[-2000:] + p.stderr[-3000:])
print(p.stdout.strip())

print(f"\n{len(passed)} checks passed")
