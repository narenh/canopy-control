"""Live Activity pushes for Canopy Control, through the canopy-apns relay.

    relay = Relay(url="https://apns.canopysf.com", key="...")      # or None: pushes off
    live = LiveActivities(DATA / "installs.json", relay, "com.canopysf.CanopyControl", lookup)

An install is one app install (an app-made UUID) of one person, with its APNs environment, its
push-to-start token, and the running activity's push token plus the TV it shows. `lookup(owner,
device_id)` gives a TV's content-state (or None if that person has no such TV).

Hooks from the TVs (all synchronous, never raise, never block: each push is its own task):
- started_playing(owner, tv): `update` installs whose activity shows that TV and is under 8 h old,
  `start` the rest (push-to-start token), at most one start per (install, TV) per 2 minutes.
- changed(owner, tv): debounced 0.5 s; `update` each install whose activity shows that TV, only if
  the content-state differs from the last one sent to that activity.
- removed(owner, tv, state): `end` (dismissed now) each activity showing that TV, and forget it.

Relay answers: `unregistered` clears the token that was used; `failed` is logged and kept
(BadDeviceToken usually means the install's environment is wrong, not a dead token).
"""
import asyncio, json, logging, re, time
from pathlib import Path
from typing import Callable

import aiohttp

DEFAULT_URL = "https://apns.canopysf.com"
DEFAULT_BUNDLE_ID = "com.canopysf.CanopyControl"
ENVIRONMENTS = ("sandbox", "production")
ATTRIBUTES_TYPE = "SpeedAttributes"
TOKEN_RE = re.compile(r"^[0-9a-fA-F]{1,200}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ACTIVITY_MAX_AGE_S = 8 * 3600  # Apple ends a Live Activity after 8 hours
START_GAP_S = 120              # one start per (install, TV) per this long
DEBOUNCE_S = 0.5
MAX_INSTALLS = 20              # per person; the least recently registered go first
TIMEOUT_S = 10
log = logging.getLogger("live-push")


def valid_token(t) -> bool:
    return isinstance(t, str) and bool(TOKEN_RE.match(t))


class Relay:
    """POST {url}/v1/liveactivity. send() returns the relay's answer dict, or None if it didn't give one."""

    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/")
        self.key = key
        self._session: aiohttp.ClientSession | None = None
        self._session_loop: asyncio.AbstractEventLoop | None = None

    async def _http(self) -> aiohttp.ClientSession:
        loop = asyncio.get_running_loop()
        if self._session is None or self._session.closed or self._session_loop is not loop:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S))
            self._session_loop = loop
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def send(self, payload: dict) -> dict | None:
        http = await self._http()
        headers = {"Authorization": f"Bearer {self.key}"}
        async with http.post(f"{self.base}/v1/liveactivity", json=payload, headers=headers) as r:
            if r.status == 200:
                body = await r.json(content_type=None)
                if isinstance(body, dict) and body.get("result") in ("delivered", "unregistered", "failed"):
                    return body
                log.warning("relay answered 200 without a result: %r", body)
                return None
            text = (await r.text())[:300]
        if r.status == 401:
            log.error("the APNs relay rejected CANOPY_APNS_KEY (401): enroll this instance's key on the relay")
        elif r.status == 403:
            log.error("the APNs relay won't push to %s for this key (403): add it to "
                      "CANOPY_APNS_LIVEACTIVITY_TOPICS on the relay", payload.get("bundle_id"))
        elif r.status == 422:
            log.error("the APNs relay refused a push (%s) as malformed (422): %s", payload.get("event"), text)
        elif r.status == 429:
            log.warning("the APNs relay is rate-limiting us (429)")
        elif r.status == 503:
            log.warning("the APNs relay has no APNs set up (503)")
        else:
            log.warning("the APNs relay answered %s: %s", r.status, text)
        return None


class LiveActivities:
    def __init__(self, path: Path, relay: Relay | None, bundle_id: str,
                 lookup: Callable[[str, str], dict | None]):
        self.path = path
        self.relay = relay  # None: pushes off, installs still stored
        self.bundle_id = bundle_id
        self.lookup = lookup
        self.clock = time.time  # replaceable in tests
        self.installs: dict[str, dict] = {}  # install_id -> {owner, environment, push_to_start_token, activity}
        self._sent: dict[str, tuple[str, dict]] = {}  # install_id -> (activity token, last content-state sent)
        self._started: dict[tuple[str, str], float] = {}  # (install_id, tv) -> when a start was last sent
        self._stamps: dict[str, int] = {}  # token -> last timestamp used, kept increasing
        self._debounce: dict[tuple[str, str], asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()

    # --- storage
    def load(self):
        try:
            raw = json.loads(self.path.read_text()) if self.path.exists() else {}
        except Exception as ex:
            log.warning("ignoring unreadable %s: %s", self.path, ex)
            raw = {}
        self.installs = {}
        for iid, e in (raw.items() if isinstance(raw, dict) else ()):
            try:
                act = e.get("activity")
                if act is not None:
                    act = {"token": act["token"], "device_id": act["device_id"], "since": float(act["since"])}
                    assert valid_token(act["token"]) and isinstance(act["device_id"], str)
                inst = {"owner": e["owner"], "environment": e.get("environment"),
                        "push_to_start_token": e.get("push_to_start_token"), "activity": act}
                assert isinstance(inst["owner"], str) and inst["owner"] and UUID_RE.match(iid)
                assert inst["environment"] in ENVIRONMENTS + (None,)
                assert inst["push_to_start_token"] is None or valid_token(inst["push_to_start_token"])
                self.installs[iid] = inst
            except Exception:
                log.warning("skipping unreadable install entry %s", str(iid)[:8])

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.installs, indent=1))

    # --- the app's PUT / DELETE (validated by the caller)
    def put(self, install_id: str, owner: str, changes: dict):
        """Apply `changes` (only the fields the app sent; None clears) to the install, then push the
        TV's current state to a newly set activity."""
        inst = self.installs.pop(install_id, None)
        if inst is None or inst["owner"] != owner:  # a new install, or another person signed in on it
            self._forget(install_id)
            inst = {"owner": owner, "environment": None, "push_to_start_token": None, "activity": None}
        for k in ("environment", "push_to_start_token"):
            if k in changes:
                inst[k] = changes[k]
        if "activity" in changes:
            a, old = changes["activity"], inst["activity"]
            if a is None:
                inst["activity"] = None
            else:
                since = old["since"] if old and old["token"] == a["token"] else self.clock()
                inst["activity"] = {"token": a["token"], "device_id": a["device_id"], "since": since}
            self._sent.pop(install_id, None)
        self.installs[install_id] = inst  # re-inserted last: most recently registered
        mine = [i for i, x in self.installs.items() if x["owner"] == owner]
        for old_id in mine[:-MAX_INSTALLS]:
            del self.installs[old_id]
            self._forget(old_id)
        self.save()
        if changes.get("activity"):
            self._update(install_id, force=True)

    def delete(self, install_id: str, owner: str) -> bool:
        inst = self.installs.get(install_id)
        if not inst or inst["owner"] != owner:
            return False
        del self.installs[install_id]
        self._forget(install_id)
        self.save()
        return True

    def _forget(self, install_id: str):
        self._sent.pop(install_id, None)
        for k in [k for k in self._started if k[0] == install_id]:
            del self._started[k]

    # --- hooks from the TVs
    def started_playing(self, owner: str, device_id: str):
        """One of this person's TVs started playing: show it on every install."""
        if not self.relay:
            return
        try:
            state = self.lookup(owner, device_id)
            if not state:
                return
            now = self.clock()
            mine = [iid for iid, inst in self.installs.items() if inst["owner"] == owner]
            if not mine:
                log.info("%s started playing: no installs registered for its owner", device_id[:8])
            for iid, inst in self.installs.items():
                if inst["owner"] != owner:
                    continue
                if inst["environment"] not in ENVIRONMENTS:
                    log.info("install %s: no environment yet, not pushing", iid[:8])
                    continue
                act = inst["activity"]
                if act and act["device_id"] == device_id and now - act["since"] < ACTIVITY_MAX_AGE_S:
                    log.info("install %s: activity already shows %s, updating", iid[:8], device_id[:8])
                    self._update(iid, force=True)
                elif not inst["push_to_start_token"]:
                    log.info("install %s: no push-to-start token, can't start an activity", iid[:8])
                else:
                    if now - self._started.get((iid, device_id), float("-inf")) < START_GAP_S:
                        log.info("install %s: not starting another activity for %s yet", iid[:8], device_id[:8])
                        continue
                    self._started[(iid, device_id)] = now
                    self._push(iid, "push_to_start_token", inst["push_to_start_token"], inst["environment"], {
                        "event": "start", "content_state": state,
                        "attributes_type": ATTRIBUTES_TYPE, "attributes": {},
                        "alert": {"title": state["deviceName"], "body": "Now playing"},
                        "relevance_score": 100})
        except Exception:
            log.exception("started_playing failed")

    def changed(self, owner: str, device_id: str):
        """Something on the activity's TV may have changed: update its activities, debounced."""
        if not self.relay:
            return
        try:
            key = (owner, device_id)
            if key not in self._debounce:
                self._debounce[key] = self._spawn(self._flush(key))
        except Exception:
            log.exception("changed failed")

    async def _flush(self, key: tuple[str, str]):
        try:
            await asyncio.sleep(DEBOUNCE_S)
        finally:
            if self._debounce.get(key) is asyncio.current_task():
                self._debounce.pop(key)
        now = self.clock()
        for iid, inst in list(self.installs.items()):
            act = inst["activity"]
            if (inst["owner"] == key[0] and act and act["device_id"] == key[1]
                    and now - act["since"] < ACTIVITY_MAX_AGE_S):
                self._update(iid)

    def removed(self, owner: str, device_id: str, state: dict):
        """The person removed this TV: end and forget the activities showing it."""
        try:
            task = self._debounce.pop((owner, device_id), None)
            if task:
                task.cancel()
            touched = False
            for iid, inst in self.installs.items():
                act = inst["activity"]
                if inst["owner"] != owner or not act or act["device_id"] != device_id:
                    continue
                if self.relay and inst["environment"] in ENVIRONMENTS:
                    self._push(iid, "activity", act["token"], inst["environment"], {
                        "event": "end", "content_state": state, "dismissal_date": int(self.clock())})
                inst["activity"] = None
                self._sent.pop(iid, None)
                touched = True
            if touched:
                self.save()
        except Exception:
            log.exception("removed failed")

    # --- sending
    def _update(self, iid: str, force=False):
        """Push the activity's TV state to this install's activity, unless it's what was sent last."""
        inst = self.installs.get(iid)
        if not self.relay or not inst or not inst["activity"] or inst["environment"] not in ENVIRONMENTS:
            return
        act = inst["activity"]
        state = self.lookup(inst["owner"], act["device_id"])
        if not state:
            return
        if not force and self._sent.get(iid) == (act["token"], state):
            return
        self._sent[iid] = (act["token"], state)
        self._push(iid, "activity", act["token"], inst["environment"],
                   {"event": "update", "content_state": state})

    def _push(self, iid: str, slot: str, token: str, environment: str, fields: dict):
        payload = {"device_token": token, "environment": environment, "bundle_id": self.bundle_id,
                   **fields, "timestamp": self._stamp(token), "priority": 10}
        self._spawn(self._send(iid, slot, payload))

    def _stamp(self, token: str) -> int:
        """Now, in unix seconds, but always later than the last push to this token: ActivityKit
        drops updates that aren't newer than the one it has."""
        if len(self._stamps) > 5000:
            self._stamps.clear()
        t = max(int(self.clock()), self._stamps.get(token, 0) + 1)
        self._stamps[token] = t
        return t

    async def _send(self, iid: str, slot: str, payload: dict):
        event, token = payload["event"], payload["device_token"]
        try:
            answer = await self.relay.send(payload)
        except Exception as ex:
            log.warning("install %s: %s push not sent: %s", iid[:8], event, str(ex) or type(ex).__name__)
            answer = None
        result = answer and answer.get("result")
        if result != "delivered" and slot == "activity" and self._sent.get(iid, ("",))[0] == token:
            self._sent.pop(iid, None)  # let the next change try again
        if result == "delivered":
            log.info("install %s: %s delivered", iid[:8], event)
        elif result == "unregistered":
            inst = self.installs.get(iid)
            if slot == "activity" and inst and inst["activity"] and inst["activity"]["token"] == token:
                inst["activity"] = None
                self.save()
            elif slot == "push_to_start_token" and inst and inst["push_to_start_token"] == token:
                inst["push_to_start_token"] = None
                self.save()
            log.info("install %s: %s token is unregistered: forgotten", iid[:8], slot.replace("_", "-"))
        elif result == "failed":
            reason = answer.get("reason")
            hint = " (is this install's environment right?)" if reason == "BadDeviceToken" else ""
            log.warning("install %s: %s push failed: %s%s", iid[:8], event, reason, hint)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("live activity push task failed", exc_info=task.exception())

    async def drain(self):
        """Wait for every pending push (and debounce) to finish."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self, grace_s: float = 2.0):
        if self._tasks:
            try:
                await asyncio.wait_for(self.drain(), grace_s)
            except asyncio.TimeoutError:
                for t in list(self._tasks):
                    t.cancel()
        if self.relay:
            await self.relay.close()
