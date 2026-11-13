"""Logging whether the TV's YouTube app is in the Lounge session (loungeStatus lists a LOUNGE_SCREEN).

    python -I tests/test_presence.py [repo]

On each connect the state is logged; in between, only a change is.
"""
import asyncio, json, logging, os, sys, tempfile
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(REPO))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("CANOPY_ACCOUNT_KEY", "cnp_test")
import main  # noqa: E402

main.RECONNECT_S = 0.05
passed = 0


def check(name, cond, info=""):
    global passed
    assert cond, f"{name}: {info}"
    passed += 1


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        msg = record.getMessage()
        if "YouTube app" in msg:
            self.lines.append(msg)


cap = Capture()
main.log.addHandler(cap)

TV = {"app": "lb-v4", "name": "Bedroom TV", "id": "tv-device-id", "type": "LOUNGE_SCREEN",
      "deviceInfo": json.dumps({"clientName": "tvhtml5"})}
US = {"app": "web", "name": "Speed Remote", "id": "remote-id-1", "type": "REMOTE_CONTROL"}
ISB = {"app": "web", "name": "Canopy Control", "id": "remote-id-2", "type": "REMOTE_CONTROL"}


def status(*devices):
    return ["loungeStatus", {"devices": json.dumps(list(devices)), "connectionEventDetails": "{}"}]


async def session(d, at_connect, during):
    """One connect whose bind reply carries `at_connect` events, then a subscription that delivers
    `during` and ends; the next connect blocks until cancelled."""
    api, n = d.api, [0]

    async def connect():
        n[0] += 1
        if n[0] > 1:
            await asyncio.Event().wait()
        if at_connect:
            await api._process_events([[i, e] for i, e in enumerate(at_connect)])
        return True

    async def subscribe():
        for i, e in enumerate(during):
            await api._process_events([[100 + i, e]])

    async def nothing(*a, **k):
        return None

    api.auth.screen_id = "screen-id-not-a-real-tv"
    api.linked = lambda: True
    api.connect, api.subscribe, api.get_now_playing = connect, subscribe, nothing
    task = asyncio.create_task(d._loop())
    await asyncio.sleep(0.3)
    task.cancel()


async def run():
    # Present at connect, gone, back, a repeat: one line each for connect and the two changes.
    d = main.Device("owner-1", "Bedroom TV")
    cap.lines.clear()
    await session(d, [status(US, TV)], [status(US, TV), status(US, ISB), status(US, ISB),
                                        status(TV, US, ISB), status(TV, US)])
    check("present at connect, then only changes", cap.lines == [
        "Bedroom TV: YouTube app in the session",
        "Bedroom TV: YouTube app NOT in the session (only remotes: Speed Remote (REMOTE_CONTROL), "
        "Canopy Control (REMOTE_CONTROL))",
        "Bedroom TV: YouTube app in the session",
    ], cap.lines)
    check("no ids in the log", not any("remote-id" in l or "tv-device-id" in l for l in cap.lines), cap.lines)
    check("pyytlounge still sees the screen", d.api.screen_name == "Bedroom TV", d.api.screen_name)

    # Absent at connect: logged at once, even though nothing changed.
    d = main.Device("owner-1", "Living Room TV")
    cap.lines.clear()
    await session(d, [status(US)], [status(US)])
    check("absent at connect is logged", cap.lines == [
        "Living Room TV: YouTube app NOT in the session (only remotes: Speed Remote (REMOTE_CONTROL))",
    ], cap.lines)

    # No loungeStatus in the connect: unknown, then the first one counts as a change.
    d = main.Device("owner-1", "Den TV")
    cap.lines.clear()
    await session(d, [], [status(US, TV)])
    check("no loungeStatus at connect: unknown, then its state", cap.lines == [
        "Den TV: YouTube app presence unknown (no loungeStatus yet)",
        "Den TV: YouTube app in the session",
    ], cap.lines)

    # A malformed loungeStatus doesn't break presence logging.
    d = main.Device("owner-1", "Odd TV")
    cap.lines.clear()
    d._note_lounge_status({"devices": [US]})  # already a list, not a string
    check("a list instead of a JSON string works", d._tv_in_session is False and d._session_remotes ==
          ["Speed Remote (REMOTE_CONTROL)"], d._session_remotes)
    check("nothing logged before a connect", cap.lines == [], cap.lines)


asyncio.run(run())
print(f"{passed} checks passed")
