import asyncio, sys, types
from pathlib import Path
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent))  # default: this repo
import main
from pyytlounge.models import State

class FakeApi:
    """Stands in for YtLoungeApi. Like the real TV, ignores languageCode="" and only turns
    captions off for setSubtitlesTrack with just a videoId."""
    def __init__(self, dev, tv_applies=True):
        self.dev, self.tv_applies, self.sent = dev, tv_applies, []
    def connected(self): return True
    changing = False
    async def set_playback_speed(self, s):
        if s == self.dev.speed: return
        self.changing = True  # like the real TV: caption commands mid-speed-change are dropped
        async def reply():
            await asyncio.sleep(0.2); self.changing = False
            await self.dev.playback_speed_changed(types.SimpleNamespace(playback_speed=s))
        asyncio.create_task(reply())
    async def set_closed_captions(self, lang, vid):
        return await self._command("setSubtitlesTrack", {"languageCode": lang or "", "videoId": vid})
    async def _command(self, cmd, params):
        assert cmd == "setSubtitlesTrack"
        self.sent.append(params)
        lang = params.get("languageCode")
        if self.tv_applies and lang != "" and not self.changing:
            async def reply():
                await asyncio.sleep(0.2)
                await self.dev.subtitles_track_changed(types.SimpleNamespace(video_id=params["videoId"], language_code=lang, kind=None))
            asyncio.create_task(reply())
        return True

P = {"id": "person-a", "emailVerified": True}

def dev(tv_applies=True):
    d = main.Device(P["id"], "TV"); d.api = FakeApi(d, tv_applies); d.video_id = "v1"
    main.devices.clear(); main.devices[(P["id"], "x")] = d
    type(d).id = property(lambda self: "x")
    return d

def play(d, vid):
    return d.now_playing_changed(types.SimpleNamespace(video_id=vid, state=State.Playing))

async def run():
    d = dev()
    # slider 1.5 -> on; slider 1 -> off (sent as videoId only), both confirmed
    assert (await main.set_speed("x", 1.5, P))["captions"] is True
    r = await main.set_speed("x", 1.0, P)
    assert r["captions"] is False and d.api.sent[-1] == {"videoId": "v1"}, (r, d.api.sent)
    # toggle on at 1x, then a new video keeps it on (last control wins)
    assert (await main.set_captions("x", "on", P))["captions"] is True
    await play(d, "v2"); await d.playback_state_changed(types.SimpleNamespace(state=State.Playing))
    assert d.api.sent[-1] == {"languageCode": "en", "videoId": "v2"} and len(d.api.sent) == 4, d.api.sent
    # slider 2x then toggle off: new video stays off
    await main.set_speed("x", 2.0, P); await main.set_captions("x", "off", P)
    await play(d, "v3")
    assert d.api.sent[-1] == {"videoId": "v3"}, d.api.sent
    # toggle off then slider 1.25: slider wins, new video gets captions
    await main.set_speed("x", 1.25, P)
    await play(d, "v4")
    assert d.api.sent[-1] == {"languageCode": "en", "videoId": "v4"}, d.api.sent
    # no control used yet -> new videos left alone
    d = dev(); await play(d, "v9"); assert d.api.sent == []
    # TV ignores the command -> 409 with detail, state unchanged
    d = dev(tv_applies=False)
    try:
        await main.set_captions("x", "on", P); raise SystemExit("expected 409")
    except main.HTTPException as e:
        assert e.status_code == 409 and d.captions is False, e.detail
    # another person can't reach this TV
    try:
        await main.set_speed("x", 1.5, {"id": "person-b"}); raise SystemExit("expected 404")
    except main.HTTPException as e:
        assert e.status_code == 404 and e.detail == "unknown device", e.detail
    print("all checks passed")

asyncio.run(run())
