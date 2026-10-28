"""yt-speed: hold YouTube Lounge sessions to TVs, per Canopy Account, and expose a tiny HTTP API.

Every route needs `Authorization: Bearer <Canopy session token>` (the iOS app's sign-in). Each
person sees and controls only the TVs they paired; two people can pair the same TV, each with
their own Lounge session. Up to MAX_DEVICES TVs per person. There is no web UI.

GET    /devices                   - the caller's TVs with connection/playback state: {"id", "name",
                                   "connected", "video_id", "title", "channel", "speed", "captions",
                                   "skip_sponsors"?}, also what pair, PATCH, speed and captions return
POST   /pair {"code": "..."}      - pair another TV via its "Link with TV code"
PATCH  /devices/{id} {"name": ..., "skip_sponsors": bool} - either or both: rename a TV (trimmed,
                                   1-40 chars; null or "" = the TV's own name), skip sponsors on it
DELETE /devices/{id}              - forget a TV (ends Live Activities showing it)
POST   /devices/{id}/speed/{x}    - x in 1, 1.25, 1.5, 1.75, 2; with the caller's auto_captions on,
                                   also captions on above 1, off at 1; off: only the speed
POST   /devices/{id}/captions/{on|off}   - last of slider/toggle wins, re-applied to each new video
GET    /me/settings               - the caller's settings: {"auto_captions": bool} (default true)
PATCH  /me/settings {"auto_captions": bool} - change them (strict bool; {} is 422); returns all of
                                   them. Stored per person in settings.json, only non-defaults.
PUT    /installs/{uuid}           - an app install's Live Activity tokens (see below)
DELETE /installs/{uuid}           - forget an install (the app's sign-out)

Errors are JSON {"detail": "<sentence>"}, plus "reason" for: 401 signed_out, 403 email_unverified,
503 account_unreachable, 409 too_many_devices, 403 skip_not_allowed. A speed or caption command
that doesn't reach the TV is a 503 (and starts a fresh Lounge session), not a 500.

Titles (oembed.py): "title" and "channel" are the TV's current video's, from YouTube's public oEmbed
endpoint, fetched in the background when the video changes and cached per video id; null until
fetched, with no video, or when oEmbed has none (private/unembeddable; retried after 10 min).

Shorts (shorts.py): when a TV's video turns out to be a Short (youtube.com/shorts/<id> answers 200,
not a redirect), it's set to 1x with captions off, and want_captions goes off too. Nothing is
restored after the Short: the next video plays at 1x without captions until someone changes it.

Skip sponsors (an iSponsorBlockTV container next to this one, isbtv.py): on when ISPONSORBLOCKTV_CONFIG
is set. Each TV in GET /devices etc. then has "skip_sponsors", except for SKIP_HIDDEN_PERSON_IDS (and
for everyone when it's off), who get 403 skip_not_allowed for PATCH {"skip_sponsors": ...}. The TVs
with it on, for anyone not hidden, are rendered into iSponsorBlockTV's config.json.

Live Activities (Canopy Control, through the canopy-apns relay; live_push.py): PUT /installs/{uuid}
takes {"environment": "sandbox"|"production", "push_to_start_token": hex, "activity": {"token":
hex, "device_id": "<one of the caller's TVs>"}}, all optional, null clears. When one of a person's
TVs starts playing (a new video, or the first Playing after the session connects or after 30 min
without), each of their installs gets an update if its activity already shows that TV (and is
under 8 h old), else a push-to-start (at most one per install and TV per 2 min). Speed, captions,
connection and name changes go to the activities showing that TV, debounced 0.5 s. When a TV stops
playing (video ended, Stopped, nowPlaying with no video, YouTube closed or the session ended) and
doesn't play again (Playing/Starting; or Paused, after a dropped session) within
LIVE_ACTIVITY_END_GRACE_S, the activities showing it are ended (dismissed now) and forgotten, as
when the TV is removed. Paused alone isn't stopping; a forced _resubscribe() doesn't count.

Env: CANOPY_ACCOUNT_KEY (required; the site key from the Account Manager's Sites tab),
CANOPY_ACCOUNT_URL (default https://account.canopysf.com), DATA_DIR (default /data),
CAPTION_LANG (default en), CANOPY_APNS_KEY (this instance's relay key; unset = no pushes),
CANOPY_APNS_URL (default https://apns.canopysf.com), CANOPY_CONTROL_BUNDLE_ID (default
com.canopysf.CanopyControl); for skip sponsors, ISPONSORBLOCKTV_CONFIG, SKIP_HIDDEN_PERSON_IDS and the
iSponsorBlockTV settings in isbtv.py.
"""
import asyncio, json, logging, os, tempfile, time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictBool, model_validator
from pyytlounge import EventListener
from pyytlounge.models import State

import canopy_account
from lounge import LoungeApi
import isbtv
import live_push
import oembed
import shorts

DATA = Path(os.environ.get("DATA_DIR", "/data"))
DEVICES_FILE = DATA / "devices.json"
SETTINGS_FILE = DATA / "settings.json"
LEGACY_AUTH = DATA / "auth.json"  # single-TV file from earlier versions, no owner: ignored
CAPTION_LANG = os.environ.get("CAPTION_LANG", "en")
CAPTION_WAIT = 1.5  # seconds to wait for the TV to confirm a speed or caption change (it takes well under 1)
RESUBSCRIBE_GAP_S = 30  # at most one forced resubscribe per TV this often
SPEEDS = {1.0, 1.25, 1.5, 1.75, 2.0}
MAX_DEVICES = 10  # TVs per person
MAX_NAME = 40  # characters in a TV name set from the app
CANOPY_ACCOUNT_URL = (os.environ.get("CANOPY_ACCOUNT_URL") or "").strip() or canopy_account.DEFAULT_URL
CANOPY_ACCOUNT_KEY = (os.environ.get("CANOPY_ACCOUNT_KEY") or "").strip()
CANOPY_APNS_URL = (os.environ.get("CANOPY_APNS_URL") or "").strip() or live_push.DEFAULT_URL
CANOPY_APNS_KEY = (os.environ.get("CANOPY_APNS_KEY") or "").strip()
CANOPY_CONTROL_BUNDLE_ID = (os.environ.get("CANOPY_CONTROL_BUNDLE_ID") or "").strip() or live_push.DEFAULT_BUNDLE_ID
RECONNECT_S = 10  # wait between failed Lounge connection attempts (a routine session end reconnects at once)
EVENT_SILENCE_S = 60  # the TV sends something at least every 30 s: this long with nothing, the session is dead
# Log every raw Lounge event from the TV (type and a truncated payload), for diagnosing what
# the TV actually reports. On by default while Live Activity starts are being debugged.
LOG_LOUNGE_EVENTS = (os.environ.get("LOG_LOUNGE_EVENTS") or "1").strip().lower() not in ("0", "false", "no", "off")
PLAY_IDLE_S = 30 * 60  # Playing again after this long without counts as starting to play
LIVE_ACTIVITY_END_GRACE_S = 60  # a TV that stopped playing has this long to play again before its activities end
log = logging.getLogger("yt-speed")
logging.basicConfig(level=logging.INFO)
ISBTV = isbtv.from_env(os.environ)  # None: skip sponsors is off
canopy = canopy_account.CanopyAccount(CANOPY_ACCOUNT_URL, CANOPY_ACCOUNT_KEY) if CANOPY_ACCOUNT_KEY else None
titles = oembed.Titles()  # the current video's title and channel, fetched in the background
shorts_check = shorts.Shorts()  # whether a video is a Short (see Device._reset_for_short)


class Device(EventListener):
    """One paired TV: its Lounge session, reconnect loop, and last-known playback state."""

    def __init__(self, owner: str, name: str = "TV", custom_name: str | None = None,
                 skip_sponsors: bool = False):
        self.owner = owner  # Canopy person id of whoever paired it
        self.name = name  # what the TV calls itself
        self.custom_name = custom_name  # what its owner renamed it to, if they did
        self.skip_sponsors = skip_sponsors  # its owner wants iSponsorBlockTV on it
        self.api = LoungeApi("Speed Remote", self, owner)  # its own remote id: see lounge.py
        if LOG_LOUNGE_EVENTS:
            self._log_raw_events()
        self.task: asyncio.Task | None = None
        self._resubscribed_at = float("-inf")
        self._video_id: str | None = None
        self.speed = 1.0
        self.captions = False  # only ever set from the TV's onSubtitlesTrackChanged
        self.want_captions: bool | None = None  # last control wins: slider (on above 1x) or toggle
        self._captions_changed = asyncio.Event()
        self._speed_changed = asyncio.Event()
        self._auto_captioned: str | None = None  # last video want_captions was applied to
        # for Live Activities: when this TV "starts playing" (see _note_state)
        self._fresh = True  # no Playing seen since the session (re)connected
        self._playing = False
        self._paused = False  # last reported state was Paused (on a video)
        self._played_video: str | None = None  # last video reported Playing
        self._stopped_at = 0.0  # when it last stopped being Playing (live.clock time)
        self._end_task: asyncio.Task | None = None  # pending end of its activities (see _stopping)
        self._end_why = ""  # what that end is for
        self._short_task: asyncio.Task | None = None  # see _check_short

    @property
    def video_id(self) -> str | None:
        return self._video_id

    @video_id.setter
    def video_id(self, v: str | None):
        if v == self._video_id:
            return
        titles.want(v)  # in the background: info() shows the title once it's known
        self._video_id = v
        self._check_short(v)
        # Kept in devices.json: after a restart a TV may not say what it's playing until something
        # changes (it answers getNowPlaying with no video), so the app would show nothing playing.
        try:
            save_devices()
        except Exception:
            log.exception("%s: couldn't save its video", self.display_name)

    # --- events from the TV
    async def now_playing_changed(self, e):
        log.info("%s now playing: video=%s state=%s", self.display_name, e.video_id, e.state.name)
        if e.video_id:
            self.video_id = e.video_id
        self._note_state(e.state, video=bool(e.video_id))
        if e.state == State.Playing:
            await self._auto_caption()

    async def playback_state_changed(self, e):
        log.info("%s state: %s (video=%s)", self.display_name, e.state.name, self.video_id)
        self._note_state(e.state)
        if e.state == State.Playing:
            await self._auto_caption()

    async def playback_speed_changed(self, e):
        self.speed = e.playback_speed
        self._speed_changed.set()
        self._changed()

    async def disconnected(self, e):
        self._changed()  # pyytlounge drops the session right after this returns; the push is debounced
        self._stopping("disconnected")

    # --- Live Activities (never raise, never block)
    def _note_state(self, state, video=True):
        """Starting to play: Playing a different video than the last one reported Playing, or the
        first Playing since the session (re)connected or after PLAY_IDLE_S without Playing.
        Stopping (see _stopping): the video ended, Stopped, or a nowPlaying with no video (`video`
        false). Paused is neither, but playing again after a pause counts as starting: if the
        Live Activity never showed up (or was swiped away), unpausing brings it back; if it's
        showing, it just gets an update (live.started_playing decides, and rate-limits starts)."""
        try:
            resumed = self._paused and state in (State.Playing, State.Starting)
            self._paused = state == State.Paused and video
            if state in (State.Playing, State.Starting):
                self._playing_again(state.name)
            elif state == State.Paused and video:
                self._playing_again("Paused", connection_only=True)
            elif state in (State.Buffering, State.Stopped) or not video:
                # pyytlounge's Buffering is YouTube's state 0, "ended" (see pyytlounge/models.py)
                self._stopping("no video" if not video else "ended" if state == State.Buffering else "stopped")
            now = live.clock()
            # Starting (YouTube's "buffering") counts too: after a session drop the TV may only
            # report a new video as Starting, with no later Playing event to catch.
            if state not in (State.Playing, State.Starting):
                if self._playing:
                    self._playing, self._stopped_at = False, now
                return
            starts = (self._fresh or self.video_id != self._played_video or resumed
                      or (not self._playing and now - self._stopped_at >= PLAY_IDLE_S))
            self._fresh, self._playing, self._played_video = False, True, self.video_id
            if starts:
                log.info("%s %s %s", self.display_name, "resumes" if resumed else "starts playing", self.video_id)
                live.started_playing(self.owner, self.id)
        except Exception:
            log.exception("%s: live activity start check failed", self.display_name)

    def _changed(self):
        try:
            live.changed(self.owner, self.id)
        except Exception:
            log.exception("%s: live activity update failed", self.display_name)

    CONNECTION = ("disconnected", "session ended", "session error")  # _stopping reasons

    def _stopping(self, why: str):
        """The TV stopped playing (why: ended, stopped, no video, or one of CONNECTION): end the
        Live Activities showing it in LIVE_ACTIVITY_END_GRACE_S unless it plays again first (see
        _playing_again). One pending end per TV: a second stop keeps the first one's deadline.
        Nothing to do with pushes off or no activity showing it; ended() ends whatever shows it
        when the grace is up."""
        try:
            if (not live.relay or (self._end_task and not self._end_task.done())
                    or not live.showing(self.owner, self.id)):  # nothing to end
                return
            grace = LIVE_ACTIVITY_END_GRACE_S
            log.info("%s stopped playing (%s): ending its Live Activities in %gs unless it plays again",
                     self.display_name, why, grace)
            self._end_why = why
            self._end_task = asyncio.get_running_loop().create_task(self._end_later(grace))
        except Exception:
            log.exception("%s: live activity end check failed", self.display_name)

    def _playing_again(self, state: str, connection_only=False):
        """Playing or Starting (any video) cancels a pending end. So does Paused on a video, but
        only an end for a dropped session (the TV came back, and Paused isn't stopping); an ended
        or stopped video has to play again."""
        task = self._end_task
        if not task or task.done() or (connection_only and self._end_why not in self.CONNECTION):
            return
        task.cancel()
        self._end_task = None
        log.info("%s reported %s within the grace period: keeping its Live Activities (was: %s)",
                 self.display_name, state, self._end_why)

    async def _end_later(self, grace: float):
        await asyncio.sleep(grace)
        if self._end_task is asyncio.current_task():
            self._end_task = None
        try:
            n = live.ended(self.owner, self.id, activity_state(self))
            log.info("%s didn't play again within %gs (%s): ended %d Live Activit%s", self.display_name, grace,
                     self._end_why, n, "y" if n == 1 else "ies")
            self._fresh = True  # its next Playing (even of the same video) starts a new activity
        except Exception:
            log.exception("%s: ending live activities failed", self.display_name)

    async def set_speed(self, speed: float):
        """Set the speed and, if it changes, wait for the TV to apply it: a caption command
        sent while the TV is still changing speed gets dropped."""
        changing = speed != self.speed
        self._speed_changed.clear()
        await self._send("speed", self.api.set_playback_speed(speed))
        if changing:
            try:
                await asyncio.wait_for(self._speed_changed.wait(), CAPTION_WAIT)
            except asyncio.TimeoutError:
                log.warning("%s: TV didn't report a speed change to %s", self.display_name, speed)
                self._resubscribe()
        self.speed = speed  # callers render the response, even if the TV's event is late
        self._changed()

    def _log_raw_events(self):
        """Wrap pyytlounge's event dispatch to log what the TV sends before it's interpreted."""
        dispatch = getattr(self.api, "_process_event", None)
        if dispatch is None:  # a pyytlounge without it (or a test fake): nothing to wrap
            return

        async def logged(event_type, args):
            if event_type != "noop":
                try:
                    payload = "" if event_type == "loungeStatus" else json.dumps(args, default=str)[:400]
                except Exception:
                    payload = "?"
                log.info("%s lounge event %s %s", self.display_name, event_type, payload)
            return await dispatch(event_type, args)

        self.api._process_event = logged

    async def subtitles_track_changed(self, e):
        log.info("%s captions: video=%s lang=%r kind=%r", self.display_name, e.video_id, e.language_code, e.kind)
        # The TV sometimes reports nowPlaying with no video (e.g. after a reconnect) while this
        # event names the one on screen.
        if e.video_id and e.video_id != self.video_id:
            log.info("%s video from captions event: %s (was %s)", self.display_name, e.video_id, self.video_id)
            self.video_id = e.video_id
        self.captions = bool(e.language_code)
        self._captions_changed.set()
        self._changed()

    async def _send(self, what: str, command):
        """Await a Lounge command. If it didn't reach the TV (pyytlounge raised: no session,
        YouTube refused it, the network failed; or returned False: YouTube said the session is
        gone), start a fresh session and raise a 503 for the route to answer with (not a bare 500)."""
        try:
            if await command is not False:
                return
            why = "the Lounge session is gone"
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            why = str(ex) or type(ex).__name__
        log.warning("%s: %s command failed: %s", self.display_name, what, why)
        self._resubscribe()
        raise HTTPException(503, f"{self.display_name} isn't answering. Reconnecting; try again in a few seconds.")

    # --- captions
    async def _send_captions(self, on: bool):
        if on:
            await self._send("captions", self.api.set_closed_captions(CAPTION_LANG, self.video_id))
        else:
            # pyytlounge sends languageCode="" for off, which the TV ignores. YouTube's own remote
            # (player remote.js) sends setSubtitlesTrack with only the videoId.
            await self._send("captions", self.api._command("setSubtitlesTrack", {"videoId": self.video_id}))

    async def _auto_caption(self):
        """The TV keeps the speed across videos but not captions, so re-apply the last choice per video."""
        if self.want_captions is None or not self.video_id or self.video_id == self._auto_captioned:
            return
        self._auto_captioned = self.video_id
        try:
            # Don't wait for the change here: the TV's reply arrives through this same event loop.
            await self._send_captions(self.want_captions)
        except Exception as ex:
            log.warning("%s auto-captions failed: %s", self.display_name, ex)

    def _check_short(self, video_id: str | None):
        """In the background (needs a running loop; without one, nothing): see _reset_for_short."""
        if not video_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._short_task = loop.create_task(self._reset_for_short(video_id))

    async def _reset_for_short(self, video_id: str):
        """A Short plays at 1x with captions off. Nothing is restored after it: the next video
        stays at 1x, and auto-captions (want_captions) stays off, as if the slider had gone to 1x."""
        try:
            if await shorts_check.is_short(video_id) is not True or self.video_id != video_id:
                return
            log.info("%s is playing a Short (%s): 1x, captions off", self.display_name, video_id)
            self.want_captions = False
            self._auto_captioned = video_id  # _auto_caption leaves this video alone
            await self.set_speed(1.0)  # sent even if we think it's 1x: our speed may be stale
            await self.set_captions(False)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.warning("%s: couldn't reset for a Short: %s", self.display_name, getattr(ex, "detail", ex))

    async def set_captions(self, on: bool):
        """Ask the TV for captions; if that's a change, wait for the TV to confirm it."""
        self.want_captions = on
        self._auto_captioned = self.video_id
        self._captions_changed.clear()
        await self._send_captions(on)
        if self.captions != on:
            try:
                await asyncio.wait_for(self._captions_changed.wait(), CAPTION_WAIT)
            except asyncio.TimeoutError:
                log.warning("%s: TV didn't report a caption change for %s", self.display_name, self.video_id)
                self._resubscribe()

    # --- lifecycle
    @property
    def display_name(self) -> str:
        return self.custom_name or self.name

    @property
    def id(self) -> str:
        return self.api.auth.screen_id

    async def start(self):
        await self.api.__aenter__()
        self.task = asyncio.create_task(self._loop())

    def _resubscribe(self):
        """A command got no confirmation. The TV applies our commands even when its events stop
        reaching us: YouTube sometimes lets a subscription go quiet a minute or more before it
        ends the session, and every command then waits out CAPTION_WAIT. Start a fresh session
        now instead of waiting for that (at most once per RESUBSCRIBE_GAP_S)."""
        now = time.monotonic()
        if now - self._resubscribed_at < RESUBSCRIBE_GAP_S or not self.task:
            return
        self._resubscribed_at = now
        log.warning("%s: events stopped arriving; starting a fresh session", self.display_name)
        self.task.cancel()
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
        if self._end_task:
            self._end_task.cancel()  # removing the TV ends its activities itself (live.removed)
        await self.api.close()

    async def _loop(self):
        """Keep connected + subscribed. Reconnects when the TV app closes or the token expires."""
        while True:
            up = False  # this iteration's session connected
            started = time.monotonic()
            try:
                if not self.api.linked():
                    await self.api.refresh_auth()
                    save_devices()
                was_connected = self.api.connected()
                if await self.api.connect():
                    up = True
                    if not was_connected:
                        self._fresh = True
                    if self.api.screen_name and self.name != self.api.screen_name:
                        self.name = self.api.screen_name
                        save_devices()
                    log.info("connected to %s (TV %s)", self.display_name, self.id[:8])
                    self._changed()
                    await self.api.get_now_playing()
                    await self._subscribe()  # blocks until the session drops
                    log.info("session ended: %s", self.display_name)
                    # Often routine: the next session's nowPlaying/Playing cancels this. Our own
                    # _resubscribe() cancels this task instead, so it never gets here.
                    self._stopping("session ended")
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                log.warning("%s session error: %s", self.display_name, ex)
                if up:
                    self._stopping("session error")
            self._changed()
            # YouTube ends sessions every few minutes. Reconnect at once after one that lasted: the
            # TV announces a video only when it starts (asked later, it may answer with no video),
            # so one that starts while we're away is missed. Wait only after a failed or short one
            # (TV off, app closed), so a session that keeps failing doesn't spin.
            if not up or time.monotonic() - started < RECONNECT_S:
                await asyncio.sleep(RECONNECT_S)

    async def _subscribe(self):
        """api.subscribe(), but give up on it once the TV has sent nothing for EVENT_SILENCE_S: a
        subscription can stay open long after its session died, and we'd hear nothing until a
        command failed (iSponsorBlockTV's watchdog does the same)."""
        sub = asyncio.ensure_future(self.api.subscribe())
        try:
            while not sub.done():
                await asyncio.wait({sub}, timeout=EVENT_SILENCE_S / 4)
                last = getattr(self.api, "last_event_at", None)
                if not sub.done() and last is not None and time.monotonic() - last > EVENT_SILENCE_S:
                    log.warning("%s: nothing from the TV for %ds; starting a fresh session",
                                self.display_name, time.monotonic() - last)
                    return
            sub.result()  # its exception, if it raised
        finally:
            sub.cancel()

    def info(self) -> dict:
        titles.want(self.video_id)  # no-op unless it's not cached yet or a failure has expired
        title, channel = titles.get(self.video_id)
        out = {"id": self.id, "name": self.display_name, "connected": self.api.connected(),
               "video_id": self.video_id, "title": title, "channel": channel,
               "speed": self.speed, "captions": self.captions}
        if skip_allowed(self.owner):  # the app shows the toggle only when this is there
            out["skip_sponsors"] = self.skip_sponsors
        return out


def skip_allowed(owner: str) -> bool:
    return ISBTV is not None and owner not in ISBTV.hidden


def activity_state(d: Device) -> dict:
    """The Live Activity content-state for a TV: SpeedAttributes.ContentState in the app."""
    return {"deviceId": d.id, "deviceName": d.display_name, "speed": d.speed, "captions": d.captions,
            "connected": d.api.connected()}


devices: dict[tuple[str, str], Device] = {}  # (owner, screen_id) -> Device


def _lookup(owner: str, device_id: str) -> dict | None:
    d = devices.get((owner, device_id))
    return activity_state(d) if d else None


live = live_push.LiveActivities(
    DATA / "installs.json",
    live_push.Relay(CANOPY_APNS_URL, CANOPY_APNS_KEY) if CANOPY_APNS_KEY else None,
    CANOPY_CONTROL_BUNDLE_ID, _lookup)


def save_devices():
    DATA.mkdir(parents=True, exist_ok=True)
    # api.store_auth_state() writes a format load_auth_state() rejects (pyytlounge 3.4 bug)
    DEVICES_FILE.write_text(json.dumps(
        [{"owner": d.owner, "name": d.name, **({"custom_name": d.custom_name} if d.custom_name else {}),
          **({"skip_sponsors": True} if d.skip_sponsors else {}),
          **({"video_id": d.video_id} if d.video_id else {}),
          "auth": d.api.auth.serialize()} for d in devices.values()],
        indent=1))
    render_isbtv()


DEFAULT_SETTINGS = {"auto_captions": True}
settings: dict[str, dict] = {}  # person id -> the settings they changed from DEFAULT_SETTINGS


def person_settings(person_id: str) -> dict:
    return {**DEFAULT_SETTINGS, **settings.get(person_id, {})}


def write_atomic(path: Path, text: str):
    """A temp file in the same directory, then os.replace: a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def save_settings():
    write_atomic(SETTINGS_FILE, json.dumps(settings, indent=1, sort_keys=True))


def load_settings():
    """settings.json: {person id: {setting: value}}, only values that differ from the defaults.
    Anything unreadable is skipped (that person gets the defaults back)."""
    settings.clear()
    try:
        raw = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
    except Exception as ex:
        log.warning("ignoring unreadable %s: %s", SETTINGS_FILE, ex)
        raw = {}
    for pid, values in (raw.items() if isinstance(raw, dict) else ()):
        if not isinstance(pid, str) or not pid or not isinstance(values, dict):
            continue
        kept = {k: v for k, v in values.items()
                if k in DEFAULT_SETTINGS and type(v) is type(DEFAULT_SETTINGS[k]) and v != DEFAULT_SETTINGS[k]}
        if kept:
            settings[pid] = kept


def render_isbtv():
    """iSponsorBlockTV's config.json from the TVs with skip sponsors on (rewritten only on a change,
    since every rewrite restarts it). Never raises: a failure here mustn't fail a pairing."""
    if ISBTV is None:
        return
    try:
        tvs = [(d.id, isbtv.label(d.id)) for d in devices.values() if d.skip_sponsors and skip_allowed(d.owner)]
        if ISBTV.write(tvs):
            log.info("iSponsorBlockTV config: %d TV(s) skipping sponsors", len(ISBTV.devices(tvs)))
    except Exception:
        log.exception("couldn't write iSponsorBlockTV's config %s", ISBTV.path)


def _normalize_auth(data: dict) -> dict:
    if "version" in data:
        return data
    return {"version": 0, "screenId": data.get("screenId"),  # old store_auth_state() format
            "loungeIdToken": data.get("lounge_id_token"),
            "refreshToken": data.get("refresh_token"), "expiry": None}


def load_devices() -> tuple[list[Device], int]:
    """The saved TVs, and how many ownerless (pre-accounts) entries were dropped."""
    entries = json.loads(DEVICES_FILE.read_text()) if DEVICES_FILE.exists() else []
    if not DEVICES_FILE.exists() and LEGACY_AUTH.exists():
        log.warning("ignoring %s from before Canopy Accounts: re-pair the TV from the app", LEGACY_AUTH)
    out, dropped = [], 0
    for entry in entries:
        owner = entry.get("owner")
        if not isinstance(owner, str) or not owner:
            dropped += 1
            continue
        custom = entry.get("custom_name")
        d = Device(owner, entry.get("name", "TV"), custom if isinstance(custom, str) and custom else None,
                   entry.get("skip_sponsors") is True)
        video = entry.get("video_id")
        d._video_id = video if isinstance(video, str) and video else None  # what it last reported
        try:
            d.api.load_auth_state(_normalize_auth(entry["auth"]))
            out.append(d)
        except Exception as ex:
            log.warning("skipping unreadable device entry (re-pair needed): %s", ex)
    if dropped:
        log.warning("dropped %d TV(s) paired before Canopy Accounts (no owner): re-pair them from the app",
                    dropped)
    return out, dropped


def check_writable(path: Path):
    """Fail at startup, not at the first pairing, if the data dir isn't writable (the image runs
    as uid 10001: a volume made by an older, root image needs a chown)."""
    probe = path / ".write-test"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok")
        probe.unlink()
    except OSError as ex:
        log.critical("%s isn't writable by uid %s (%s): refusing to start. Give the volume to this "
                     "user (chown -R 10001:10001) or start from an empty volume.", path, os.getuid(), ex)
        raise RuntimeError(f"{path} isn't writable") from ex


@asynccontextmanager
async def lifespan(_):
    if canopy is None:
        log.critical("CANOPY_ACCOUNT_KEY is not set: refusing to start. Make a site in the Account "
                     "Manager's Sites tab and set its key as CANOPY_ACCOUNT_KEY.")
        raise RuntimeError("CANOPY_ACCOUNT_KEY is not set")
    log.info("Canopy Accounts at %s", canopy.base)
    if live.relay:
        log.info("Live Activity pushes for %s via %s", live.bundle_id, live.relay.base)
    else:
        log.info("CANOPY_APNS_KEY is not set: Live Activity pushes are off (installs are still stored)")
    if ISBTV:
        log.info("skip sponsors on: iSponsorBlockTV config at %s, %d hidden person(s)", ISBTV.path,
                 len(ISBTV.hidden))
    else:
        log.info("ISPONSORBLOCKTV_CONFIG is not set: skip sponsors is off")
    check_writable(DATA)
    live.load()
    load_settings()
    loaded, dropped = load_devices()
    for d in loaded:
        devices[(d.owner, d.id)] = d
    if dropped:
        save_devices()  # rewrite without the ownerless entries
    render_isbtv()  # also drops TVs of people added to SKIP_HIDDEN_PERSON_IDS since the last run
    for d in devices.values():
        await d.start()
    yield
    for d in devices.values():
        await d.stop()
    await live.close()
    await titles.close()
    await shorts_check.close()
    await canopy.close()


app = FastAPI(lifespan=lifespan)


class ApiError(HTTPException):
    """An HTTPException that also says why, as a machine-readable `reason`."""

    def __init__(self, status: int, detail: str, reason: str, headers: dict | None = None):
        super().__init__(status, detail, headers)
        self.reason = reason


@app.exception_handler(ApiError)
async def api_error(_: Request, ex: ApiError):
    return JSONResponse({"detail": ex.detail, "reason": ex.reason}, ex.status_code, headers=ex.headers)


async def require_person(request: Request) -> dict:
    """The signed-in Canopy person calling, from `Authorization: Bearer <token>`. Fails closed."""
    signed_out = ApiError(401, "Sign in with your Canopy Account.", "signed_out", {"WWW-Authenticate": "Bearer"})
    token = canopy_account.read_token(request.headers.get("authorization"))
    if not token:
        raise signed_out
    if canopy is None:  # only if started without the lifespan check
        raise ApiError(503, "Canopy Accounts isn't set up on this server.", "account_unreachable")
    try:
        answer = await canopy.who_is(token)
    except canopy_account.Unreachable:
        raise ApiError(503, "Canopy Accounts could not be reached. Try again in a minute.",
                       "account_unreachable")
    person = answer["person"]
    if answer["unverified"] or (person and person.get("emailVerified") is False):
        raise ApiError(403, "Confirm your email with Canopy Accounts first.", "email_unverified")
    if not person:
        raise signed_out
    return person


def get(owner: str, device_id: str, need_video=False) -> Device:
    d = devices.get((owner, device_id))
    if not d:
        raise HTTPException(404, "unknown device")
    if not d.api.connected():
        raise HTTPException(503, f"{d.display_name} not connected (is YouTube open on it?)")
    if need_video and not d.video_id:
        raise HTTPException(409, "no video playing")
    return d


class Pair(BaseModel):
    code: str


@app.get("/devices")
async def list_devices(person: dict = Depends(require_person)):
    return [d.info() for d in devices.values() if d.owner == person["id"]]


@app.post("/pair")
async def pair(body: Pair, person: dict = Depends(require_person)):
    owner = person["id"]
    d = Device(owner)
    await d.api.__aenter__()
    if not await d.api.pair(body.code.replace(" ", "")):
        await d.api.close()
        raise HTTPException(400, "pairing failed")
    d.name = d.api.screen_name or "TV"
    key = (owner, d.id)
    # Checked after pairing (only then is the TV known), so re-pairing a TV at the limit works.
    # No await between this check and the insert, so concurrent pairs can't overshoot.
    if key not in devices and sum(x.owner == owner for x in devices.values()) >= MAX_DEVICES:
        await d.api.close()
        raise ApiError(409, f"You can pair up to {MAX_DEVICES} TVs. Remove one first.", "too_many_devices")
    old = devices.get(key)  # re-pairing the same TV replaces this person's entry
    devices[key] = d
    if old:
        d.custom_name = old.custom_name  # but keeps their name for it
        d.skip_sponsors = old.skip_sponsors  # and their skip choice
        await old.stop()
    else:
        d.skip_sponsors = skip_allowed(owner)  # on by default for a new TV; turning it off is deliberate
    d.task = asyncio.create_task(d._loop())
    save_devices()
    return d.info()


class DevicePatch(BaseModel):
    """At least one of the two. name: null or "" goes back to the TV's own name."""
    name: str | None = None
    skip_sponsors: StrictBool | None = None  # true or false; null is refused below

    @model_validator(mode="after")
    def _some(self):
        if not self.model_fields_set:
            raise ValueError("send name, skip_sponsors or both")
        if "skip_sponsors" in self.model_fields_set and self.skip_sponsors is None:
            raise ValueError("skip_sponsors must be true or false")
        return self


@app.patch("/devices/{device_id}")
async def patch_device(device_id: str, body: DevicePatch, person: dict = Depends(require_person)):
    d = devices.get((person["id"], device_id))
    if not d:
        raise HTTPException(404, "unknown device")
    sent = body.model_fields_set
    # everything is checked before anything changes, so a refused PATCH changes nothing
    if "skip_sponsors" in sent and not skip_allowed(person["id"]):
        raise ApiError(403, "Skipping sponsors isn't available for this account.", "skip_not_allowed")
    if "name" in sent:
        name = None if body.name in (None, "") else body.name.strip()
        if name is not None and not 1 <= len(name) <= MAX_NAME:
            raise HTTPException(400, f"name must be 1 to {MAX_NAME} characters (or empty to use the TV's own name)")
        d.custom_name = name
    if "skip_sponsors" in sent:
        d.skip_sponsors = body.skip_sponsors
    save_devices()
    if "name" in sent:
        d._changed()
    return d.info()


@app.delete("/devices/{device_id}")
async def remove(device_id: str, person: dict = Depends(require_person)):
    d = devices.pop((person["id"], device_id), None)
    if not d:
        raise HTTPException(404, "unknown device")
    state = activity_state(d)
    await d.stop()
    save_devices()
    live.removed(person["id"], device_id, state)
    return {"removed": device_id}


@app.post("/devices/{device_id}/speed/{speed}")
async def set_speed(device_id: str, speed: float, person: dict = Depends(require_person)):
    if speed not in SPEEDS:
        raise HTTPException(400, f"speed must be one of {sorted(SPEEDS)}")
    d = get(person["id"], device_id)
    # The sender's setting (d is theirs). Off: only the speed changes; no caption command, and
    # want_captions stays as the last control (the toggle, or the slider while it was on) left it.
    auto = person_settings(person["id"])["auto_captions"]
    await d.set_speed(speed)
    if auto:
        if d.video_id:
            await d.set_captions(speed > 1)
        else:
            d.want_captions = speed > 1  # applied when the next video starts
    return d.info()


@app.post("/devices/{device_id}/captions/{mode}")
async def set_captions(device_id: str, mode: str, person: dict = Depends(require_person)):
    if mode not in ("on", "off"):
        raise HTTPException(400, "mode must be on or off")
    d = get(person["id"], device_id, need_video=True)
    await d.set_captions(mode == "on")
    if d.captions != (mode == "on"):
        hint = f" (does this video have '{CAPTION_LANG}' captions?)" if mode == "on" else ""
        # not 502: Cloudflare replaces 502 bodies with its own page, hiding the detail
        raise HTTPException(409, f"{d.display_name} didn't turn captions {mode}{hint}")
    return d.info()


class SettingsPatch(BaseModel):
    auto_captions: StrictBool | None = None  # true or false; null is refused below

    @model_validator(mode="after")
    def _some(self):
        if not self.model_fields_set:
            raise ValueError("send auto_captions")
        if self.auto_captions is None:
            raise ValueError("auto_captions must be true or false")
        return self


@app.get("/me/settings")
async def get_settings(person: dict = Depends(require_person)):
    return person_settings(person["id"])


@app.patch("/me/settings")
async def patch_settings(body: SettingsPatch, person: dict = Depends(require_person)):
    pid = person["id"]
    new = {**person_settings(pid), **{k: getattr(body, k) for k in body.model_fields_set}}
    changed = {k: v for k, v in new.items() if v != DEFAULT_SETTINGS[k]}
    if changed != settings.get(pid, {}):
        old = settings.get(pid)
        if changed:
            settings[pid] = changed
        else:
            settings.pop(pid, None)
        try:
            save_settings()
        except BaseException:
            if old is None:
                settings.pop(pid, None)
            else:
                settings[pid] = old
            raise
    return person_settings(pid)


class ActivityBody(BaseModel):
    token: str
    device_id: str


class InstallBody(BaseModel):
    """All optional: an absent field is left as is, null clears it."""
    environment: str | None = None
    push_to_start_token: str | None = None
    activity: ActivityBody | None = None


def _install_id(install_id: str) -> str:
    if not live_push.UUID_RE.match(install_id):
        raise HTTPException(400, "install_id must be a UUID")
    return install_id.lower()


@app.put("/installs/{install_id}")
async def put_install(install_id: str, body: InstallBody, person: dict = Depends(require_person)):
    install_id = _install_id(install_id)
    owner = person["id"]
    changes = {k: getattr(body, k) for k in body.model_fields_set}
    if changes.get("environment") is not None and changes["environment"] not in live_push.ENVIRONMENTS:
        raise HTTPException(400, "environment must be sandbox or production")
    if changes.get("push_to_start_token") is not None:
        if not live_push.valid_token(changes["push_to_start_token"]):
            raise HTTPException(400, "push_to_start_token must be hex, up to 200 characters")
        changes["push_to_start_token"] = changes["push_to_start_token"].lower()
    if changes.get("activity") is not None:
        a = changes["activity"]
        if not live_push.valid_token(a.token):
            raise HTTPException(400, "activity.token must be hex, up to 200 characters")
        if (owner, a.device_id) not in devices:
            raise HTTPException(404, "unknown device")
        changes["activity"] = {"token": a.token.lower(), "device_id": a.device_id}
    live.put(install_id, owner, changes)
    return {"ok": True}


@app.delete("/installs/{install_id}")
async def delete_install(install_id: str, person: dict = Depends(require_person)):
    if not live.delete(_install_id(install_id), person["id"]):
        raise HTTPException(404, "unknown install")
    return {"ok": True}
