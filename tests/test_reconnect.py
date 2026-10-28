"""Staying subscribed the way iSponsorBlockTV does: reconnect at once after a routine session end,
give up on a session the TV has gone quiet on, and send commands one at a time.

    python -I tests/test_reconnect.py [repo]
"""
import asyncio, os, sys, tempfile, time, types
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(REPO))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("CANOPY_ACCOUNT_KEY", "cnp_test")
import main  # noqa: E402
import lounge  # noqa: E402
from pyytlounge import YtLoungeApi  # noqa: E402

main.RECONNECT_S = 0.3
main.EVENT_SILENCE_S = 0.4
main.SHORT_BACKOFF_S = 0.6
passed = 0


def check(name, cond, info=""):
    global passed
    assert cond, f"{name}: {info}"
    passed += 1


class FakeLounge:
    """Each session lasts `session_s`, or forever if None; `quiet` stops last_event_at moving."""
    def __init__(self, session_s=None, quiet=False):
        self.auth = types.SimpleNamespace(screen_id="scr-tv")
        self.session_s, self.quiet = session_s, quiet
        self.connects = []
        self.subs_cancelled = 0
        self.last_event_at = time.monotonic()
        self.screen_name = None

    def linked(self): return True
    def connected(self): return bool(self.connects)
    async def connect(self):
        self.connects.append(time.monotonic())
        self.last_event_at = time.monotonic()
        return True
    async def get_now_playing(self): pass

    async def subscribe(self):
        try:
            end = time.monotonic() + (self.session_s if self.session_s is not None else 3600)
            while time.monotonic() < end:
                if not self.quiet:
                    self.last_event_at = time.monotonic()  # the TV's keepalive noops
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            self.subs_cancelled += 1
            raise


async def run_device(api, seconds):
    d = main.Device("owner-1", "TV")
    d.api = api
    d.task = asyncio.create_task(d._loop())
    await asyncio.sleep(seconds)
    d.task.cancel()


async def run():
    # A session that lasted (YouTube's routine end): the next connect follows at once.
    api = FakeLounge(session_s=0.35)
    await run_device(api, 1.0)
    gaps = [b - a for a, b in zip(api.connects, api.connects[1:])]
    check("reconnects after a routine session end", len(api.connects) >= 2, api.connects)
    check("without waiting RECONNECT_S", all(g < 0.35 + 0.15 for g in gaps), gaps)

    # A session that ends at once (TV off, app closed): wait between attempts, never spin.
    api = FakeLounge(session_s=0)
    await run_device(api, 1.0)
    gaps = [b - a for a, b in zip(api.connects, api.connects[1:])]
    check("a session that ends at once waits RECONNECT_S", gaps and all(g >= main.RECONNECT_S for g in gaps), gaps)

    # The subscription stays open but the TV goes quiet: give up on it and connect again.
    api = FakeLounge(quiet=True)
    await run_device(api, 1.0)
    check("a quiet session is dropped", api.subs_cancelled >= 1, api.subs_cancelled)
    check("and a fresh one connected", len(api.connects) >= 2, api.connects)

    # A live session (keepalives arriving) is left alone.
    api = FakeLounge()
    await run_device(api, 1.0)
    check("a live session is kept", len(api.connects) == 1, api.connects)

    # A Short ends the session (disconnectedByUserScreenInitiated): stay away SHORT_BACKOFF_S.
    d = main.Device("owner-1", "TV")
    api = d.api = FakeLounge(session_s=0.35)
    plain_subscribe = api.subscribe

    async def kicked_by_short():
        await plain_subscribe()
        if len(api.connects) == 1:
            await d.disconnected(types.SimpleNamespace(reason="disconnectedByUserScreenInitiated"))

    api.subscribe = kicked_by_short
    d.task = asyncio.create_task(d._loop())
    await asyncio.sleep(1.2)
    d.task.cancel()
    gap = api.connects[1] - api.connects[0] if len(api.connects) > 1 else None
    check("after a Short, reconnects only after SHORT_BACKOFF_S", gap is not None and gap >= 0.35 + main.SHORT_BACKOFF_S - 0.05, api.connects)

    # lounge.LoungeApi: commands one at a time, and any events move last_event_at.
    order, inflight = [], [0]

    async def slow_command(self, command, command_parameters=None):
        inflight[0] += 1
        order.append((command, inflight[0]))
        await asyncio.sleep(0.02)
        inflight[0] -= 1
        return True

    async def no_events(self, events): pass

    YtLoungeApi._command = slow_command
    YtLoungeApi._process_events = no_events
    a = lounge.LoungeApi("Speed Remote", object(), "person-a")
    await asyncio.gather(a._command("setPlaybackSpeed"), a._command("getNowPlaying"), a._command("setSubtitlesTrack"))
    check("commands go out one at a time", [n for _, n in order] == [1, 1, 1], order)
    before = a.last_event_at
    await asyncio.sleep(0.01)
    await a._process_events([[1, ["noop"]]])
    check("a noop counts as hearing from the TV", a.last_event_at > before)


asyncio.run(run())
print(f"{passed} checks passed")
