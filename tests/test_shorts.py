"""A Short plays at 1x with captions off, and nothing is restored after it.

    python -I tests/test_shorts.py [repo]
"""
import asyncio, os, sys, tempfile, types
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(REPO))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("CANOPY_ACCOUNT_KEY", "cnp_test")
import main  # noqa: E402
import shorts  # noqa: E402

main.CAPTION_WAIT = 0.2
SHORT, LONG, UNKNOWN = "1FLHigSK3TQ", "26p-npt4fTI", "aaaaaaaaaaa"
ANSWERS = {SHORT: True, LONG: False, UNKNOWN: None}
asked = []


async def fake_fetch(vid):
    asked.append(vid)
    await asyncio.sleep(0.01)
    return ANSWERS[vid]


async def no_title(vid):
    return None, None


main.shorts_check.fetch = fake_fetch
main.titles.fetch = no_title
passed = 0


def check(name, cond, info=""):
    global passed
    assert cond, f"{name}: {info}"
    passed += 1


class TV:
    """Applies speed and caption commands and reports them back, like the real TV."""
    def __init__(self, d):
        self.d, self.sent = d, []
        self.auth = types.SimpleNamespace(screen_id="scr-tv")

    def connected(self): return True

    async def set_playback_speed(self, s):
        self.sent.append(("speed", s))
        asyncio.get_running_loop().call_soon(
            lambda: asyncio.ensure_future(self.d.playback_speed_changed(types.SimpleNamespace(playback_speed=s))))

    async def set_closed_captions(self, lang, vid):
        self.sent.append(("cc", lang))
        self._report(vid, lang)

    async def _command(self, cmd, params):
        self.sent.append((cmd, params))
        if cmd == "setSubtitlesTrack":
            self._report(params.get("videoId"), params.get("languageCode") or None)

    def _report(self, vid, lang):
        e = types.SimpleNamespace(video_id=vid, language_code=lang, kind=None)
        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.d.subtitles_track_changed(e)))


async def settle():
    await asyncio.sleep(0.15)


async def run():
    d = main.Device("owner-1", "TV")
    tv = d.api = TV(d)
    d.speed, d.captions, d.want_captions = 1.5, True, True

    d.video_id = LONG
    await settle()
    check("a regular video is left alone", tv.sent == [] and d.speed == 1.5 and d.want_captions, tv.sent)

    d.video_id = SHORT
    await settle()
    check("a Short goes to 1x", ("speed", 1.0) in tv.sent and d.speed == 1.0, (tv.sent, d.speed))
    check("captions off", d.captions is False and d.want_captions is False, (d.captions, d.want_captions))
    check("speed before captions", [c for c, *_ in tv.sent].index("speed") < [c for c, *_ in tv.sent].index("setSubtitlesTrack"), tv.sent)

    tv.sent.clear()
    await d._auto_caption()
    check("auto-captions leaves the Short alone", tv.sent == [], tv.sent)

    d.video_id = LONG
    await settle()
    await d._auto_caption()
    check("nothing restored after it: still 1x", d.speed == 1.0 and ("speed", 1.5) not in tv.sent, tv.sent)
    check("and captions stay off", d.want_captions is False and ("cc", "en") not in tv.sent, tv.sent)

    tv.sent.clear()
    d.video_id = SHORT
    await settle()
    check("a cached answer isn't asked again", asked.count(SHORT) == 1, asked)
    check("but the reset is sent again", ("speed", 1.0) in tv.sent, tv.sent)

    tv.sent.clear()
    d.video_id = UNKNOWN
    await settle()
    check("couldn't tell: nothing sent", tv.sent == [], tv.sent)

    # The video changed before the answer came back: nothing sent for the old one.
    tv.sent.clear()
    main.shorts_check.cache.clear()
    d.video_id = SHORT
    d.video_id = LONG
    await settle()
    check("a stale answer is ignored", tv.sent == [], tv.sent)

    # The TV doesn't answer: logged, no exception escapes.
    async def broken(s): raise RuntimeError("no session")
    tv.set_playback_speed = broken
    d.video_id = SHORT
    await settle()
    check("a failed reset doesn't raise", d._short_task.done() and d._short_task.exception() is None)

    # shorts.Shorts itself: ids that aren't video ids aren't fetched, None isn't cached.
    s = shorts.Shorts(fetch=fake_fetch)
    asked.clear()
    check("not a video id", await s.is_short("v1") is None and asked == [])
    await s.is_short(UNKNOWN); await s.is_short(UNKNOWN)
    check("None isn't cached", asked == [UNKNOWN, UNKNOWN], asked)

    async def raises(vid): raise OSError("timeout")
    check("a failure is None", await shorts.Shorts(fetch=raises).is_short(SHORT) is None)


asyncio.run(run())
print(f"{passed} checks passed")
