"""Play/pause: "playing" follows what the TV reports, and the command waits for the TV to confirm.

    python -I tests/test_playback.py [repo]
"""
import asyncio, os, sys, tempfile, types
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(REPO))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("CANOPY_ACCOUNT_KEY", "cnp_test")
import main  # noqa: E402
from pyytlounge.models import State  # noqa: E402

main.CAPTION_WAIT = 0.2
main.live.changed = lambda owner, tv: changes.append(tv)
main.live.started_playing = lambda owner, tv: None
changes = []
passed = 0


def check(name, cond, info=""):
    global passed
    assert cond, f"{name}: {info}"
    passed += 1


class TV:
    """Plays and pauses, and reports it back unless `quiet`."""
    def __init__(self, d):
        self.d, self.sent, self.quiet = d, [], False
        self.auth = types.SimpleNamespace(screen_id="scr-tv")

    def connected(self): return True
    async def play(self): return self._do("play", State.Playing)
    async def pause(self): return self._do("pause", State.Paused)

    def _do(self, cmd, state):
        self.sent.append(cmd)
        if not self.quiet:
            asyncio.get_running_loop().call_later(
                0.02, lambda: asyncio.ensure_future(self.d.playback_state_changed(types.SimpleNamespace(state=state))))
        return True


def now_playing(video, state):
    return types.SimpleNamespace(video_id=video, state=state)


async def run():
    d = main.Device("owner-1", "TV")
    tv = d.api = TV(d)
    check("unknown until the TV says", d.playing is None and d.info()["playing"] is None)

    await d.playback_state_changed(types.SimpleNamespace(state=State.Playing))
    check("Playing: playing", d.playing is True and main.activity_state(d)["playing"] is True)
    check("a change is pushed", changes == ["scr-tv"], changes)
    await d.playback_state_changed(types.SimpleNamespace(state=State.Playing))
    check("no change, no push", changes == ["scr-tv"], changes)

    await d.playback_state_changed(types.SimpleNamespace(state=State.Paused))
    check("Paused: not playing", d.playing is False)
    await d.playback_state_changed(types.SimpleNamespace(state=State.Starting))
    check("Starting: playing", d.playing is True)
    await d.playback_state_changed(types.SimpleNamespace(state=State.Buffering))  # YouTube's "ended"
    check("ended: not playing", d.playing is False)

    await d.now_playing_changed(now_playing("vid-1", State.Playing))
    check("nowPlaying with a video sets it", d.playing is True)
    await d.now_playing_changed(now_playing(None, State.Stopped))
    check("nowPlaying with no video doesn't (the TV says that while playing)", d.playing is True)

    await d.set_playing(False)
    check("pause is sent and confirmed", tv.sent == ["pause"] and d.playing is False, (tv.sent, d.playing))
    await d.set_playing(True)
    check("play is sent and confirmed", tv.sent == ["pause", "play"] and d.playing is True)

    tv.quiet = True
    await d.set_playing(False)
    check("unconfirmed: the answer says what was asked for", d.playing is False)


asyncio.run(run())
print(f"{passed} checks passed")
