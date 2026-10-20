"""A command the TV never confirms (its events stopped reaching us) starts a fresh session.

    python -I tests/test_resubscribe.py [repo]
"""
import asyncio, os, sys, tempfile, types
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(REPO))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("CANOPY_ACCOUNT_KEY", "cnp_test")
import main  # noqa: E402

main.CAPTION_WAIT = 0.1
passed = 0


def check(name, cond, info=""):
    global passed
    assert cond, f"{name}: {info}"
    passed += 1


class QuietLounge:
    """The TV applies commands but no events come back."""
    def __init__(self):
        self.sent = []
        self.auth = types.SimpleNamespace(screen_id="scr-quiet")

    def connected(self): return True
    async def set_playback_speed(self, s): self.sent.append(("speed", s))
    async def set_closed_captions(self, lang, vid): self.sent.append(("cc", lang))
    async def _command(self, cmd, params): self.sent.append((cmd, params))


async def run():
    loops = []

    async def fake_loop(self):
        loops.append(self)
        await asyncio.sleep(3600)

    main.Device._loop = fake_loop
    d = main.Device("owner-1", "TV")
    d.api = QuietLounge()
    d.video_id = "abcdefghijk"
    d.task = asyncio.create_task(d._loop())
    await asyncio.sleep(0)
    first = d.task

    await d.set_speed(1.5)
    await asyncio.sleep(0)
    check("an unconfirmed speed change starts a fresh session", d.task is not first and first.cancelled(), d.task)
    check("the speed still answers as requested", d.speed == 1.5)
    second = d.task

    await d.set_captions(True)
    await asyncio.sleep(0)
    check("a second one within the gap doesn't restart it again", d.task is second and not second.cancelled())

    d._resubscribed_at -= main.RESUBSCRIBE_GAP_S
    await d.set_speed(2.0)
    await asyncio.sleep(0)
    check("after the gap it can restart again", d.task is not second and second.cancelled())

    # A confirmed change doesn't restart anything.
    d._resubscribed_at -= main.RESUBSCRIBE_GAP_S
    third = d.task

    async def confirm():
        await asyncio.sleep(0.01)
        await d.playback_speed_changed(types.SimpleNamespace(playback_speed=1.25))

    asyncio.create_task(confirm())
    await d.set_speed(1.25)
    await asyncio.sleep(0)
    check("a confirmed change keeps the session", d.task is third and not third.cancelled())
    d.task.cancel()


asyncio.run(run())
print(f"{passed} checks passed")
