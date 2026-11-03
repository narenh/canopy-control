"""Autoplay guard: a different video right after one ended is paused once, when it plays.

    python -I tests/test_autoplay.py [repo]
"""
import asyncio, logging, os, sys, tempfile, types
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(REPO))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("CANOPY_ACCOUNT_KEY", "cnp_test")
import main  # noqa: E402
from pyytlounge.models import State  # noqa: E402

main.CAPTION_WAIT = 0.2
main.live.changed = lambda owner, tv: None
main.live.started_playing = lambda owner, tv: None


async def not_short(vid): return False
async def no_title(vid): return None, None


main.shorts_check.fetch = not_short
main.titles.fetch = no_title
OLD, NEW, OTHER = "oldoldold01", "newnewnew01", "otherother1"
passed = 0


def check(name, cond, info=""):
    global passed
    assert cond, f"{name}: {info}"
    passed += 1


class Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


logs = Logs()
main.log.addHandler(logs)


class TV:
    """Pauses and plays, and reports it back, like the real TV."""
    def __init__(self, d):
        self.d, self.sent = d, []
        self.auth = types.SimpleNamespace(screen_id="scr-tv")

    def connected(self): return True
    async def play(self): return self._do("play", State.Playing)
    async def pause(self): return self._do("pause", State.Paused)

    def _do(self, cmd, state):
        self.sent.append(cmd)
        asyncio.get_running_loop().call_later(
            0.02, lambda: asyncio.ensure_future(self.d.playback_state_changed(types.SimpleNamespace(state=state))))
        return True


def device(video=OLD):
    d = main.Device("owner-1", "Bedroom")
    tv = d.api = TV(d)
    d._video_id = video
    return d, tv


async def state(d, s):
    await d.playback_state_changed(types.SimpleNamespace(state=s))


async def now_playing(d, video, s):
    await d.now_playing_changed(types.SimpleNamespace(video_id=video, state=s))


async def ended(d):
    """What the TV sends when a video ends: onStateChange state 0, twice."""
    await state(d, State.Buffering)
    await state(d, State.Buffering)


async def loads(d, video):
    """What the TV sends for the next video, as logged: nowPlaying Starting, Stopped, Starting,
    nowPlaying Starting, then Playing (onStateChange and nowPlaying)."""
    await now_playing(d, video, State.Starting)
    await state(d, State.Stopped)
    await state(d, State.Starting)
    await now_playing(d, video, State.Starting)
    await state(d, State.Playing)
    await now_playing(d, video, State.Playing)


async def settle():
    await asyncio.sleep(0.3)


async def run():
    # Ended, then a different video within the window: paused once, when it plays.
    d, tv = device()
    await state(d, State.Playing)
    await ended(d)
    await now_playing(d, NEW, State.Starting)
    await state(d, State.Stopped)
    await state(d, State.Starting)
    await asyncio.sleep(0.05)
    check("not paused while it's still Starting", tv.sent == [], tv.sent)
    await state(d, State.Playing)
    await now_playing(d, NEW, State.Playing)
    await state(d, State.Playing)
    await settle()
    check("autoplayed: one pause", tv.sent == ["pause"], tv.sent)
    check("and it's paused", d.playing is False, d.playing)
    check("logged", any(l == f"Bedroom autoplayed {NEW} after {OLD} ended: pausing it" for l in logs.lines)
          and any(l == f"Bedroom paused autoplayed {NEW}" for l in logs.lines), logs.lines)
    await state(d, State.Playing)  # someone presses play
    await settle()
    check("only once per autoplayed video", tv.sent == ["pause"], tv.sent)

    # The new video named first by a captions event (it sets video_id before nowPlaying does).
    d, tv = device()
    await ended(d)
    await d.subtitles_track_changed(types.SimpleNamespace(video_id=NEW, language_code=None, kind=None))
    await loads(d, NEW)
    await settle()
    check("autoplay seen through a captions event", tv.sent == ["pause"], tv.sent)

    # Ended, then a new video after the window: left alone.
    main.AUTOPLAY_WINDOW_S = 0.1
    d, tv = device()
    await ended(d)
    await asyncio.sleep(0.2)
    await loads(d, NEW)
    await settle()
    check("after the window: nothing", tv.sent == [], tv.sent)
    main.AUTOPLAY_WINDOW_S = 5

    # A new video with no end before it: left alone.
    d, tv = device()
    await state(d, State.Playing)
    await loads(d, NEW)
    await settle()
    check("no end: nothing", tv.sent == [], tv.sent)

    # No video known when it ended: left alone.
    d, tv = device(None)
    await ended(d)
    await loads(d, NEW)
    await settle()
    check("no ended video: nothing", tv.sent == [], tv.sent)

    # The same video starting again: left alone.
    d, tv = device()
    await ended(d)
    await loads(d, OLD)
    await settle()
    check("same video again: nothing", tv.sent == [], tv.sent)

    # One of our commands between the end and the new video: left alone.
    d, tv = device()
    await ended(d)
    await d.set_playing(True)
    tv.sent.clear()
    await loads(d, NEW)
    await settle()
    check("after our own command: nothing", tv.sent == [], tv.sent)

    # It doesn't play within the pause window: left alone.
    main.AUTOPLAY_PAUSE_WINDOW_S = 0.1
    d, tv = device()
    await ended(d)
    await now_playing(d, NEW, State.Starting)
    await asyncio.sleep(0.2)
    await state(d, State.Playing)
    await settle()
    check("played too late: nothing", tv.sent == [], tv.sent)
    main.AUTOPLAY_PAUSE_WINDOW_S = 15

    # A second video change before it plays: the first one's pause is dropped, the end used up.
    d, tv = device()
    await ended(d)
    await now_playing(d, NEW, State.Starting)
    await loads(d, OTHER)
    await settle()
    check("a second change isn't an autoplay", tv.sent == [], tv.sent)

    # Guard off: left alone.
    main.AUTOPLAY_GUARD = False
    d, tv = device()
    await ended(d)
    await loads(d, NEW)
    await settle()
    check("guard off: nothing", tv.sent == [], tv.sent)
    main.AUTOPLAY_GUARD = True

    # The pause fails: logged, nothing escapes into the event handler or the task.
    d, tv = device()
    async def broken(): raise RuntimeError("no session")
    tv.pause = broken
    logs.lines.clear()
    await ended(d)
    await loads(d, NEW)  # would raise here if the handler did
    await settle()
    check("a failed pause doesn't raise", d._autoplay_task.done() and d._autoplay_task.exception() is None)
    check("and is logged", any(l.startswith(f"Bedroom: couldn't pause autoplayed {NEW}") for l in logs.lines),
          logs.lines)


asyncio.run(run())
print(f"{passed} checks passed")
