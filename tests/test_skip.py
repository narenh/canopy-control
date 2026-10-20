"""Skip sponsors: the per-TV toggle, who may use it, and iSponsorBlockTV's rendered config.json.

python -I tests/test_skip.py [repo path, default: this repo]   (also runs itself with `off`)

A fake account service runs on 127.0.0.1, pyytlounge's YtLoungeApi is replaced with a fake, and
DATA_DIR and the config path are temp dirs. Nothing touches a real TV or account service.
"""
import asyncio, json, os, socket, stat, subprocess, sys, tempfile, threading
from pathlib import Path

from aiohttp import web

REPO = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent)  # default: this repo
OFF = sys.argv[2:] == ["off"]
sys.path.insert(0, REPO)

# ---------- fake account service
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

# ---------- data: A has the living room TV with skip on; HID's TV has skip on from before HID was hidden
DATA = Path(tempfile.mkdtemp())
CONF = Path(tempfile.mkdtemp()) / "isbtv" / "config.json"  # its directory doesn't exist yet
(DATA / "devices.json").write_text(json.dumps([
    {"owner": A, "name": "TV LIV", "custom_name": "Living", "skip_sponsors": True,
     "auth": {"version": 1, "screenId": "scr-LIV"}},
    {"owner": HID, "name": "TV HID", "skip_sponsors": True, "auth": {"version": 1, "screenId": "scr-HID"}},
]))
for k in ("SKIP_CATEGORIES", "SKIP_ADS", "MUTE_ADS", "AUTO_PLAY", "SKIP_MINIMUM_LENGTH", "SKIP_JOIN_NAME",
          "SKIP_COUNT_TRACKING", "ISPONSORBLOCKTV_CONFIG", "SKIP_HIDDEN_PERSON_IDS"):
    os.environ.pop(k, None)
os.environ.update(DATA_DIR=str(DATA), CANOPY_ACCOUNT_URL=f"http://127.0.0.1:{PORT}", CANOPY_ACCOUNT_KEY=SITE_KEY)
os.environ.pop("CANOPY_APNS_KEY", None)
if not OFF:
    os.environ.update(ISPONSORBLOCKTV_CONFIG=str(CONF), SKIP_HIDDEN_PERSON_IDS=f" {HID} , ,other-id")

import isbtv  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class FakeAuth:
    def __init__(self): self.screen_id = None
    def serialize(self): return {"version": 1, "screenId": self.screen_id}


class FakeLounge:
    def __init__(self, name, listener=None):
        self.listener, self.auth, self.screen_name, self.closed = listener, FakeAuth(), None, False
    async def __aenter__(self): return self
    async def pair(self, code):
        self.auth.screen_id, self.screen_name = f"scr-{code}", f"TV {code}"
        return True
    def load_auth_state(self, data): self.auth.screen_id = data["screenId"]
    def linked(self): return True
    async def connect(self): return False
    def connected(self): return not self.closed
    async def close(self): self.closed = True
    async def set_playback_speed(self, s):
        async def reply():
            await asyncio.sleep(0.05)
            await self.listener.playback_speed_changed(type("E", (), {"playback_speed": s}))
        asyncio.create_task(reply())


main.YtLoungeApi = FakeLounge
passed = []


def check(name, cond, info=""):
    assert cond, f"{name}: {info}"
    passed.append(name)


def H(k): return {"Authorization": f"Bearer {TOK[k]}"}


def saved(): return json.loads((DATA / "devices.json").read_text())


INFO_KEYS = {"id", "name", "connected", "video_id", "speed", "captions"}


def run_off():
    with TestClient(main.app) as c:
        check("off: ISBTV is None", main.ISBTV is None)
        for who in "AH":
            r = c.get("/devices", headers=H(who))
            check(f"off: GET /devices for {who} has no skip_sponsors", r.status_code == 200 and r.json()
                  and all(set(x) == INFO_KEYS for x in r.json()), r.text)
        r = c.post("/pair", json={"code": "NEW"}, headers=H("A"))
        check("off: POST /pair has no skip_sponsors", r.status_code == 200 and set(r.json()) == INFO_KEYS, r.text)
        for body in ({"skip_sponsors": True}, {"skip_sponsors": False}, {"name": "X", "skip_sponsors": True}):
            r = c.patch("/devices/scr-LIV", json=body, headers=H("A"))
            check(f"off: PATCH {body} is 403 skip_not_allowed", r.status_code == 403
                  and r.json().get("reason") == "skip_not_allowed" and isinstance(r.json().get("detail"), str), r.text)
        check("off: a refused PATCH changed nothing", main.devices[(A, "scr-LIV")].custom_name == "Living")
        r = c.patch("/devices/scr-LIV", json={"name": "Den"}, headers=H("A"))
        check("off: rename still works, without the field", r.status_code == 200 and r.json()["name"] == "Den"
              and set(r.json()) == INFO_KEYS, r.text)
        check("off: stored choices are kept", next(e for e in saved() if e["owner"] == A and
                                                   e["auth"]["screenId"] == "scr-LIV").get("skip_sponsors") is True)
        check("off: nothing rendered", not CONF.exists() and not CONF.parent.exists())
    print(f"off: {len(passed)} checks passed")


if OFF:
    run_off()
    sys.exit(0)

# ---------- iSponsorBlockTV's config format (v2.11.0: src/iSponsorBlockTV/helpers.py Config, and
# config.json.template, whose keys are exactly these)
UPSTREAM_KEYS = {"devices", "skip_categories", "skip_count_tracking", "mute_ads", "skip_ads",
                 "minimum_skip_length", "auto_play", "join_name", "apikey", "channel_whitelist", "use_proxy"}
DEFAULTS = {"skip_categories": ["sponsor", "selfpromo", "music_offtopic"], "skip_count_tracking": True,
            "mute_ads": False, "skip_ads": True, "minimum_skip_length": 1, "auto_play": False,
            "join_name": "Canopy Control", "apikey": "", "channel_whitelist": [], "use_proxy": False}


def conf(): return json.loads(CONF.read_text())


def devs(): return conf()["devices"]


def ino(): st = CONF.stat(); return (st.st_ino, st.st_mtime_ns)


def upstream_accepts(cfg):
    """What iSponsorBlockTV's Config.validate and Device do with it, without its packages."""
    assert set(cfg) <= UPSTREAM_KEYS, set(cfg) - UPSTREAM_KEYS
    assert cfg["devices"], "upstream exits with no devices (the wrapper doesn't start it then)"
    for d in cfg["devices"]:
        assert set(d) == {"screen_id", "name", "offset"} and d["screen_id"] and isinstance(d["name"], str)
        assert d["offset"] / 1000 == 0
    assert not (not cfg["apikey"] and cfg["channel_whitelist"])
    assert isinstance(cfg["skip_categories"], list) and cfg["skip_categories"]
    return True


with TestClient(main.app) as c:
    def patch(dev, body, who="A"): return c.patch(f"/devices/{dev}", json=body, headers=H(who))
    def tvs(who): return {x["id"]: x for x in c.get("/devices", headers=H(who)).json()}

    # --- startup render
    check("hidden ids parsed (trimmed, empties dropped)", main.ISBTV.hidden == {HID, "other-id"}, main.ISBTV.hidden)
    check("rendered at startup (directory created)", CONF.exists())
    cfg = conf()
    check("config is exactly the defaults plus devices", cfg == {"devices": [
        {"screen_id": "scr-LIV", "name": "Living", "offset": 0}], **DEFAULTS}, cfg)
    check("config keys are iSponsorBlockTV's (the template's)", set(cfg) == UPSTREAM_KEYS, set(cfg) ^ UPSTREAM_KEYS)
    check("upstream would accept it", upstream_accepts(cfg))
    check("hidden person's stored skip_sponsors left out at startup", "scr-HID" not in [d["screen_id"] for d in devs()])
    check("config is 0640", stat.S_IMODE(CONF.stat().st_mode) == 0o640, oct(CONF.stat().st_mode))
    check("config ends in a newline, 4-space JSON", CONF.read_text().endswith("}\n") and '\n    "devices"' in CONF.read_text())

    # --- the field
    r = c.get("/devices", headers=H("A"))
    check("A's TV has skip_sponsors true", r.json() == [dict(r.json()[0], skip_sponsors=True)]
          and set(r.json()[0]) == INFO_KEYS | {"skip_sponsors"}, r.text)
    r = c.get("/devices", headers=H("H"))
    check("hidden person: no skip_sponsors in GET", r.status_code == 200 and [set(x) for x in r.json()] == [INFO_KEYS], r.text)
    r = c.post("/pair", json={"code": "HX"}, headers=H("H"))
    check("hidden person: no skip_sponsors from POST /pair", r.status_code == 200 and set(r.json()) == INFO_KEYS, r.text)
    r = patch("scr-HX", {"name": "Mine"}, "H")
    check("hidden person: rename works, no skip_sponsors", r.status_code == 200 and set(r.json()) == INFO_KEYS
          and r.json()["name"] == "Mine", r.text)
    r = c.post("/devices/scr-HX/speed/1.5", headers=H("H"))
    check("hidden person: no skip_sponsors from speed", r.status_code == 200 and set(r.json()) == INFO_KEYS, r.text)

    # --- 403 for hidden people, changing nothing
    before = ino()
    for body in ({"skip_sponsors": True}, {"skip_sponsors": False}, {"name": "Other", "skip_sponsors": True}):
        r = patch("scr-HX", body, "H")
        check(f"hidden person: PATCH {body} is 403 skip_not_allowed", r.status_code == 403 and r.json() == {
            "detail": r.json()["detail"], "reason": "skip_not_allowed"} and isinstance(r.json()["detail"], str), r.text)
    check("refused PATCH kept the name", main.devices[(HID, "scr-HX")].custom_name == "Mine")
    check("refused PATCH left skip off and the config alone",
          main.devices[(HID, "scr-HX")].skip_sponsors is False and ino() == before)
    check("hidden person: unknown TV is still 404", patch("scr-nope", {"skip_sponsors": True}, "H").status_code == 404)

    # --- B turns it on, for a new TV and the TV A also has
    r = c.post("/pair", json={"code": "BED"}, headers=H("B"))
    check("new TV: skip_sponsors false", r.status_code == 200 and r.json()["skip_sponsors"] is False, r.text)
    r = c.post("/pair", json={"code": "LIV"}, headers=H("B"))
    check("B pairs A's TV too: off for B", r.json()["skip_sponsors"] is False and tvs("A")["scr-LIV"]["skip_sponsors"] is True)
    check("pairing a TV with skip off didn't rewrite the config", ino() == before)
    r = patch("scr-BED", {"skip_sponsors": True}, "B")
    check("PATCH skip_sponsors alone returns the TV", r.status_code == 200 and r.json()["skip_sponsors"] is True
          and set(r.json()) == INFO_KEYS | {"skip_sponsors"} and r.json()["name"] == "TV BED", r.text)
    check("stored as skip_sponsors true", next(e for e in saved() if e["owner"] == B and e["auth"]["screenId"] == "scr-BED")
          ["skip_sponsors"] is True)
    check("B's TV added to the config", [d["screen_id"] for d in devs()] == ["scr-LIV", "scr-BED"], devs())
    r = patch("scr-LIV", {"name": "Lounge", "skip_sponsors": True}, "B")
    check("PATCH name and skip_sponsors together", r.status_code == 200 and r.json()["name"] == "Lounge"
          and r.json()["skip_sponsors"] is True, r.text)
    check("union dedups the shared TV; first owner's name wins", devs() == [
        {"screen_id": "scr-LIV", "name": "Living", "offset": 0}, {"screen_id": "scr-BED", "name": "TV BED", "offset": 0}], devs())

    # --- rewritten only on a change
    before = ino()
    patch("scr-LIV", {"skip_sponsors": True}, "B")
    patch("scr-LIV", {"name": "Lounge 2"}, "B")  # not the name used for it
    c.post("/devices/scr-LIV/speed/1.5", headers=H("A"))
    c.post("/pair", json={"code": "HY"}, headers=H("H"))
    check("same content: not rewritten (inode and mtime unchanged)", ino() == before)
    patch("scr-LIV", {"name": "Living room"}, "A")
    check("the used name changes: rewritten", devs()[0]["name"] == "Living room" and ino() != before, devs())

    # --- turning it off / removing
    patch("scr-LIV", {"skip_sponsors": False}, "A")
    check("A off, B still on: the TV stays, named by B now", devs() == [
        {"screen_id": "scr-BED", "name": "TV BED", "offset": 0},
        {"screen_id": "scr-LIV", "name": "Lounge 2", "offset": 0}], devs())
    check("A's entry no longer stores it", "skip_sponsors" not in next(
        e for e in saved() if e["owner"] == A and e["auth"]["screenId"] == "scr-LIV"))
    check("A sees false", tvs("A")["scr-LIV"]["skip_sponsors"] is False)
    c.delete("/devices/scr-LIV", headers=H("B"))
    check("B removes it too: gone from the config", [d["screen_id"] for d in devs()] == ["scr-BED"], devs())
    r = patch("scr-BED", {"skip_sponsors": False}, "B")
    check("last TV off: devices empty", r.json()["skip_sponsors"] is False and devs() == [] and conf() == {"devices": [], **DEFAULTS})
    patch("scr-BED", {"skip_sponsors": True}, "B")
    c.post("/pair", json={"code": "BED"}, headers=H("B"))
    check("re-pairing keeps skip_sponsors", tvs("B")["scr-BED"]["skip_sponsors"] is True and [d["screen_id"] for d in devs()] == ["scr-BED"])
    c.delete("/devices/scr-BED", headers=H("B"))
    check("DELETE of a TV with skip on removes it", devs() == [])

    # --- validation
    for body in ({}, {"skip_sponsors": None}, {"skip_sponsors": "yes"}, {"skip_sponsors": 1}, {"skip_sponsors": "true"}):
        r = patch("scr-LIV", body)
        check(f"PATCH {body} is 422", r.status_code == 422, r.text)
    r = patch("scr-LIV", {"name": "x" * 41, "skip_sponsors": True})
    check("bad name with skip: 400, nothing changed", r.status_code == 400 and tvs("A")["scr-LIV"]["skip_sponsors"] is False, r.text)
    check("another person's TV is 404", patch("scr-HX", {"skip_sponsors": True}).status_code == 404)
    check("PATCH needs sign-in", c.patch("/devices/scr-LIV", json={"skip_sponsors": True}).status_code == 401)

    # --- atomic write
    calls, real_replace = [], isbtv.os.replace
    def spy(src, dst):
        calls.append((src, dst, Path(src).read_text()))
        return real_replace(src, dst)
    isbtv.os.replace = spy
    try:
        patch("scr-LIV", {"skip_sponsors": True})
    finally:
        isbtv.os.replace = real_replace
    src, dst, text = calls[0] if calls else (None, None, None)
    check("written through os.replace from a temp file in the same directory",
          len(calls) == 1 and Path(src).parent == CONF.parent and Path(dst) == CONF and Path(src).name.startswith(".config."), calls)
    check("the temp file was complete before the replace", json.loads(text)["devices"][0]["screen_id"] == "scr-LIV")
    check("no temp files left", sorted(p.name for p in CONF.parent.iterdir()) == ["config.json"], list(CONF.parent.iterdir()))

    def boom(src, dst): raise OSError("disk full")
    isbtv.os.replace = boom
    try:
        before_text = CONF.read_text()
        r = patch("scr-LIV", {"skip_sponsors": False})
    finally:
        isbtv.os.replace = real_replace
    check("a failed config write doesn't fail the PATCH", r.status_code == 200 and r.json()["skip_sponsors"] is False, r.text)
    check("a failed write leaves the old file and no temp file", CONF.read_text() == before_text
          and sorted(p.name for p in CONF.parent.iterdir()) == ["config.json"])
    patch("scr-LIV", {"skip_sponsors": True})
    check("the next change writes it", [d["screen_id"] for d in devs()] == ["scr-LIV"])
    c.post("/pair", json={"code": "BED"}, headers=H("B"))
    patch("scr-BED", {"skip_sponsors": True}, "B")
    final = CONF.read_text()

# ---------- restart: same content, not rewritten; then B is hidden and drops out
main.devices.clear()
before = ino()
with TestClient(main.app) as c:
    check("restart: devices.json choices reloaded", {k: d.skip_sponsors for k, d in main.devices.items() if k[0] != HID} == {
        (A, "scr-LIV"): True, (B, "scr-BED"): True}, {k: d.skip_sponsors for k, d in main.devices.items()})
    check("restart: same config, not rewritten", CONF.read_text() == final and ino() == before)
main.devices.clear()
main.ISBTV.hidden.add(B)  # someone added to SKIP_HIDDEN_PERSON_IDS (read at startup)
with TestClient(main.app) as c:
    check("newly hidden person's TVs drop out at startup", [d["screen_id"] for d in devs()] == ["scr-LIV"], devs())
    r = c.get("/devices", headers=H("B"))
    check("and they no longer see the field", [set(x) for x in r.json()] == [INFO_KEYS], r.text)
    check("but their choice is kept in devices.json", next(e for e in saved() if e["owner"] == B)["skip_sponsors"] is True)

# ---------- settings from the environment
cfg = isbtv.from_env({"ISPONSORBLOCKTV_CONFIG": "/x/config.json", "SKIP_CATEGORIES": " sponsor, intro ,sponsor,",
                      "SKIP_ADS": "false", "MUTE_ADS": "TRUE", "AUTO_PLAY": "1", "SKIP_MINIMUM_LENGTH": "2.5",
                      "SKIP_JOIN_NAME": "Den remote", "SKIP_COUNT_TRACKING": "no"})
check("env settings, in upstream key names", cfg.settings == dict(
    DEFAULTS, skip_categories=["sponsor", "intro"], skip_ads=False, mute_ads=True, auto_play=True,
    minimum_skip_length=2.5, join_name="Den remote", skip_count_tracking=False), cfg.settings)
check("empty env values are the defaults", isbtv.from_env({"ISPONSORBLOCKTV_CONFIG": "/x", "SKIP_CATEGORIES": "",
                                                          "SKIP_ADS": " "}).settings == DEFAULTS)
check("no ISPONSORBLOCKTV_CONFIG: off", isbtv.from_env({"SKIP_ADS": "true"}) is None and isbtv.from_env(
    {"ISPONSORBLOCKTV_CONFIG": "  "}) is None)
for bad in ({"SKIP_ADS": "maybe"}, {"SKIP_MINIMUM_LENGTH": "abc"}, {"SKIP_MINIMUM_LENGTH": "-1"}):
    try:
        isbtv.from_env({"ISPONSORBLOCKTV_CONFIG": "/x", **bad}); ok = False
    except ValueError:
        ok = True
    check(f"{bad} refused", ok)
check("devices: first name per screen id wins, empty ids dropped", isbtv.Config.devices(
    [("s1", "One"), ("", "none"), ("s2", "Two"), ("s1", "Uno")]) == [
    {"screen_id": "s1", "name": "One", "offset": 0}, {"screen_id": "s2", "name": "Two", "offset": 0}])

# ---------- feature off (a fresh process: config is read at import)
p = subprocess.run([sys.executable, "-I", __file__, REPO, "off"], capture_output=True, text=True, timeout=60)
check("feature-off run passed", p.returncode == 0 and "checks passed" in p.stdout, p.stdout[-2000:] + p.stderr[-3000:])
print(p.stdout.strip())

print(f"\n{len(passed)} checks passed")
