# canopy-control

Self-hosted playback-speed remote for YouTube on a TV, for the Canopy Control iOS app. Uses the unofficial YouTube Lounge API (via `pyytlounge`) to hold sessions to paired TVs. Runs as one Docker Compose app: this server (`api`) and an unmodified upstream [iSponsorBlockTV](https://github.com/dmunozv04/iSponsorBlockTV) (`isponsorblocktv`) that skips sponsors on the TVs people turn it on for.

## Sign-in and per-person TVs

Every route needs a Canopy Account: the app sends `Authorization: Bearer <token>` (its Canopy session token), and the server checks it with the account service (`GET /api/session`, cached 60 s; a cached answer up to 15 min old stands in if the account service is down). People whose email isn't proven aren't let in.

Each person sees and controls only the TVs they paired. Two people can pair the same TV; each gets their own Lounge session. Up to 10 TVs per person. Another person's TV, or an unknown id, is a 404.

There is no web UI any more. `/docs` and `/openapi.json` are FastAPI's.

## API

- `GET /devices` — your TVs: `[{"id", "name", "connected", "video_id", "title", "channel", "speed", "captions", "skip_sponsors"?}]`. `title` and `channel` (string or `null`) describe `video_id`: see [Video titles](#video-titles). `skip_sponsors` (bool) is there only when you may use [skip sponsors](#skip-sponsors); the app shows the toggle only then. The same TV object comes back from `/pair`, `PATCH`, `/speed` and `/captions`.
- `POST /pair {"code": "..."}` — add a TV (YouTube → Settings → Link with TV code); returns that TV. 400 `pairing failed`; 409 `too_many_devices` past 10. Re-pairing a TV you already have replaces it.
- `PATCH /devices/{id} {"name": "...", "skip_sponsors": true|false}` — either field or both (neither: 422); returns that TV. Nothing changes if any part is refused.
  - `name`: rename the TV. Trimmed, 1–40 characters (400 otherwise); `null` or `""` goes back to the name the TV reports. The custom name is yours only, survives reconnects and re-pairing, and is what `GET /devices` and Live Activities show.
  - `skip_sponsors`: turn skipping sponsors on this TV on or off, for you (kept across re-pairing). A strict boolean (422 otherwise). 403 `skip_not_allowed` if you may not use it, or it's off on this server.
- `DELETE /devices/{id}` — forget a TV: `{"removed": id}`. Live Activities showing it are ended.
- `POST /devices/{id}/speed/{x}` — x in 1, 1.25, 1.5, 1.75, 2. With your `auto_captions` setting on (the default), also captions on above 1x, off at 1x (`CAPTION_LANG`, default `en`). With it off, only the speed changes: no caption command, and the caption choice re-applied to new videos stays as it was.
- `POST /devices/{id}/captions/{on|off}` — manual caption toggle, whatever `auto_captions` is; 409 if the TV does not confirm the change. Whichever of slider (while `auto_captions` is on) and toggle was used last wins and is re-applied to each new video
- `GET /me/settings` — your settings: `{"auto_captions": true}` (the default).
- `PATCH /me/settings {"auto_captions": true|false}` — change them; returns the full settings object. A strict boolean; `{}`, `null` or another type is 422. Stored per person in `settings.json` in `/data` (only values that differ from the defaults; written atomically). The setting is the caller's: on a TV two people paired, each person's speed requests follow their own.

Errors are `{"detail": "<sentence>"}`; the auth and limit ones add `"reason"`:

| status | reason | when |
|---|---|---|
| 401 | `signed_out` | no token, a malformed one, or signed out |
| 403 | `email_unverified` | the account's email isn't proven |
| 503 | `account_unreachable` | the account service can't be reached and nothing recent is cached |
| 409 | `too_many_devices` | pairing an 11th TV |
| 403 | `skip_not_allowed` | `PATCH` with `skip_sponsors` by a hidden person, or with the feature off |

Others (no `reason`): 404 `unknown device`, 404 `unknown install`, 503 `<TV> not connected (is YouTube open on it?)`, 503 `<TV> isn't answering…` (a speed or caption command didn't reach the TV: YouTube had dropped the session, refused the command, or couldn't be reached; the server starts a fresh session, so trying again in a few seconds usually works), 409 `no video playing`, 409 `<TV> didn't turn captions on/off…`, 400 for a bad speed, mode or pairing code.

## Video titles

`title` and `channel` come from YouTube's public oEmbed endpoint (`https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=<id>&format=json`, fields `title` and `author_name`; no key). `oembed.py`:

- Fetched in the background when a TV's `video_id` changes (and when a TV is shown whose video isn't cached), never while answering a request: the response has whatever is cached, so both are `null` until the fetch is done, and when there's no video.
- Only for ids matching `^[A-Za-z0-9_-]{11}$`. 5 s timeout. One fetch per id at a time.
- Cached per video id in memory, the 500 most recently used.
- A failure (e.g. 401/403/404 for a private or unembeddable video, a timeout) is logged once at INFO and cached as `null` for 10 minutes, then fetched again the next time the id is wanted.
- Not part of the Live Activity content-state.

## Live Activities

Canopy Control (`com.canopysf.CanopyControl`) shows at most one Live Activity, for one TV. The server keeps it current with pushes through the canopy-apns relay (`POST {CANOPY_APNS_URL}/v1/liveactivity`).

- `PUT /installs/{install_id}` — `install_id` is the app install's UUID. Body, all fields optional (absent = unchanged, `null` = cleared):
  ```
  {"environment": "sandbox" | "production",
   "push_to_start_token": "<hex>",
   "activity": {"token": "<hex>", "device_id": "<one of your TVs>"}}
  ```
  Tokens are hex, up to 200 characters (400 otherwise); an `activity.device_id` that isn't one of your TVs is 404 `unknown device`. Answers `{"ok": true}`. Setting `activity` pushes that TV's current state to it right away. An install belongs to whoever registered it; another person using the same id replaces it (sign-out/sign-in on one phone). Up to 20 installs per person; past that the least recently registered is dropped.
- `DELETE /installs/{install_id}` — the app's sign-out: `{"ok": true}`, or 404 `unknown install` if it isn't yours.

Pushes:

- **A TV starts playing** — it reports Playing for a different video than the last one it played, or the first Playing after its session (re)connects (this includes a server restart), or after 30 min without Playing. Each of the owner's installs with an environment then gets an `update` if its activity already shows that TV and is under 8 h old (Apple's limit), otherwise a push-to-start (`SpeedAttributes`, alert "<TV name> / Now playing", relevance 100). The app adopts the new activity, ends any older one, and reports the new token with PUT. At most one start per install and TV every 2 minutes.
- **State changes** — speed, captions, connected and name changes on a TV are pushed as an `update` to every activity showing it, debounced 0.5 s, and only when the content-state differs from the last one sent to that activity.
- **TV removed** — activities showing it get an `end` (dismissed now) and are forgotten.
- **A TV stops playing** — its video ended (YouTube's state 0, which pyytlounge calls `Buffering`), it reports `Stopped` or a `nowPlaying` with no video, YouTube on it closed (`loungeScreenDisconnected`), or its Lounge session ended. If it doesn't play again within `LIVE_ACTIVITY_END_GRACE_S` (60 s), the activities showing it get the same `end` as for a removed TV (final content-state, dismissed now) and are forgotten; the TV's next Playing then push-starts a fresh one (not held back by the 2-minute start limit). Playing or Starting within the grace, of the same video or a new one, cancels the end and things carry on as usual. Paused is not stopping, and a TV that comes back Paused after its session dropped keeps its activity too (an ended or stopped video has to play again). Our own forced resubscribe (an unconfirmed command) doesn't count; a routine session cycle does, and is cancelled by the next session's Playing. One pending end per TV and person; a second stop keeps the first deadline. Nothing is scheduled when no activity shows the TV. Each decision is logged at INFO (`stopped playing …`, `… within the grace period: keeping …`, `didn't play again … ended N`).
- The relay's `unregistered` clears the token that was used. `failed` is logged and the token kept: `BadDeviceToken` usually means the install's `environment` is wrong.

Content-state, matching the app's Swift type: `{"deviceId", "deviceName", "speed", "captions", "connected"}`. Installs persist in `installs.json` in `/data`.

Ops, once per server instance:

1. Enroll an instance key for this server on the canopy-apns relay.
2. On the relay, allow it to push Live Activities to the app: `CANOPY_APNS_LIVEACTIVITY_TOPICS=<instance_id>:com.canopysf.CanopyControl`.
3. Set the key as `CANOPY_APNS_KEY` in Coolify and redeploy. Without it the server runs with pushes off (one INFO line at startup) and still stores installs.

## Skip sponsors

On when `ISPONSORBLOCKTV_CONFIG` is set (the compose file sets it to `/isbtv/config.json`); off otherwise, e.g. when the plain Dockerfile is deployed on its own.

- Each person's choice is stored per TV in `devices.json` (`"skip_sponsors": true`).
- iSponsorBlockTV's `devices` are the TVs with it on for anyone not hidden, once each (`{"screen_id", "name", "offset": 0}`, named with the first such owner's name for it). The other keys come from the environment, in iSponsorBlockTV's own names (see `isbtv.py`).
- `api` rewrites `config.json` when that content changes (pairing, removing, toggling, or a name change of a TV in it) and at startup, atomically (temp file in the same directory, then `os.replace`), and only when the content differs, since each rewrite restarts iSponsorBlockTV.
- **Hidden people** (`SKIP_HIDDEN_PERSON_IDS`, comma-separated Canopy person ids; the App Review account goes here) never get the field, get 403 `skip_not_allowed`, and their TVs are left out of iSponsorBlockTV. The list is read at startup: adding someone drops their TVs at the next restart (their stored choice is kept).
- The old, separate iSponsorBlockTV instance doesn't know about this one: a TV handled by both gets every skip twice. Remove it there before turning it on here.

### How the two containers fit together

- `api` (this repo's Dockerfile) is the only one exposed. It mounts the `isbtv-config` volume read-write at `/isbtv` and owns `config.json` in it.
- `isponsorblocktv` is `ghcr.io/dmunozv04/isponsorblocktv`, pinned to an exact tag and digest, not rebuilt. No ports, outbound internet only, the same volume mounted read-only at `/app/data`. Its own pyytlounge never meets ours (pyytlounge==3.4.0).
- iSponsorBlockTV reads its config only at startup, so its compose `entrypoint` is a short Python wrapper (inline in `docker-compose.yml`) and the image's real entrypoint (`python3 -u main.pyc`) is the `command` it runs as a child. It polls `config.json` every 3 s (mtime, size, inode) and restarts the child on a change; it doesn't start it while the config lists no devices (upstream would exit after 10 s); it restarts it if it exits (backoff 5 s doubling to 5 min); and on SIGTERM it forwards SIGTERM and exits.
- No Docker socket anywhere. Both services: `user: 10001:10001` (the same uid/gid, so `config.json` can be 0640), `read_only: true` with a tmpfs `/tmp`, `cap_drop: [ALL]`, `no-new-privileges`, `restart: unless-stopped`.

### Updates (Renovate)

`renovate.json` (install the Renovate GitHub app on the repo):

- **iSponsorBlockTV image**: Renovate opens a PR for every new release (patch, minor or major; and digest changes) and **auto-merges it once CI passes**. Automerge relies on the `CI` workflow's status checks: Renovate merges itself (`platformAutomerge: false`) only when they're green, never while pending or red. Since merging to `main` deploys, CI includes a compose smoke test with the new image. Making `CI` a required check on `main` (branch protection) is a good extra guard.
- **Python dependencies**: one grouped PR, never auto-merged (only pyytlounge is pinned; an update needs a real-TV check).
- The api image's base and the GitHub Actions: PRs for review, not auto-merged.

### CI

`.github/workflows/ci.yml`, on every push and PR: installs `requirements.txt` + httpx + pyyaml and runs every script in `tests/`, validates `docker compose config`, builds the api image from the Dockerfile alone, and runs `tests/smoke_compose.sh` (the compose app with the real upstream image: hardening applied, 401 from `api`, iSponsorBlockTV idle without devices, started and restarted on a config change, clean stop).

Locally: `for t in tests/test_*.py tests/check.py; do python -I "$t" || break; done` (each takes the repo path as an optional argument, default this repo), and `tests/smoke_compose.sh` with Docker.

## Setup

The account admin makes a site for this server in the Account Manager's **Sites** tab: no contact fields granted, **Allows quick (unverified) accounts** off. Its key goes in Coolify as `CANOPY_ACCOUNT_KEY`.

Environment:

- `CANOPY_ACCOUNT_KEY` — required. Without it the server refuses to start (it never runs unauthenticated).
- `CANOPY_ACCOUNT_URL` — default `https://account.canopysf.com`
- `CAPTION_LANG` — default `en`
- `DATA_DIR` — default `/data`
- `CANOPY_APNS_KEY` — optional; this instance's key on the canopy-apns relay. Unset: no Live Activity pushes
- `CANOPY_APNS_URL` — default `https://apns.canopysf.com`
- `CANOPY_CONTROL_BUNDLE_ID` — default `com.canopysf.CanopyControl`
- `ISPONSORBLOCKTV_CONFIG` — path of iSponsorBlockTV's `config.json`; set means skip sponsors is on. The compose file sets it.
- `SKIP_HIDDEN_PERSON_IDS` — comma-separated Canopy person ids who don't get skip sponsors (App Review)
- iSponsorBlockTV's settings, written into its config: `SKIP_CATEGORIES` (default `sponsor,selfpromo,music_offtopic`) → `skip_categories`; `SKIP_ADS` (default `true`) → `skip_ads`; `MUTE_ADS` (`false`) → `mute_ads`; `AUTO_PLAY` (`false`) → `auto_play`; `SKIP_MINIMUM_LENGTH` (`1`, seconds) → `minimum_skip_length`. Fixed: `join_name` "Canopy Control" (`SKIP_JOIN_NAME` to change it), `skip_count_tracking` true (`SKIP_COUNT_TRACKING`), `apikey` "", `channel_whitelist` []. A value that can't be right (e.g. `SKIP_ADS=maybe`) stops the server at startup.

The api image runs as uid 10001 and refuses to start if `/data` isn't writable by it (a volume made by the older, root image needs `chown -R 10001:10001`, or start from an empty one).

Locally (nothing is published on the host; add a `ports: ["127.0.0.1:8000:8000"]` to `api` in a `docker-compose.override.yml` to reach it):

```
CANOPY_ACCOUNT_KEY=cnp_... docker compose up -d --build
```

Pairings persist in `devices.json` on the `api-data` volume (`/data`), each with its owner's Canopy person id, any custom name (`custom_name`, next to the TV's own `name`) and `skip_sponsors` when on. Entries from before accounts (no owner), and an old single-TV `auth.json`, are dropped at startup with a warning: re-pair those TVs from the app.

### Coolify: switching to the Docker Compose build pack

Until this is done, pushing `main` still builds the plain Dockerfile: skip sponsors is then off (no `ISPONSORBLOCKTV_CONFIG`), and the api runs as uid 10001, so a `/data` storage that the old root image wrote to must be chowned or emptied first (the server says so in its log and won't start otherwise).

1. In the application: **Configuration → General → Build Pack**: choose **Docker Compose**. Base Directory `/`, Docker Compose Location `/docker-compose.yml`. Save, then **Reload Compose File** so Coolify reads the two services.
2. **Domains**: for `api`, `https://control.canopysf.com:8000` (the `:8000` is the container port Coolify's proxy routes to; nothing is published on the host). Leave `isponsorblocktv` with no domain.
3. **Environment Variables** (Coolify lists the `${...}` ones from the compose file; all of them go to `api`, `isponsorblocktv` takes none):
   - `CANOPY_ACCOUNT_KEY` — required (the same value as now)
   - `CANOPY_APNS_KEY` — the same value as now
   - `SKIP_HIDDEN_PERSON_IDS` — the App Review account's Canopy person id (appstore@canopysf.com)
   - optional, with the defaults above: `CANOPY_ACCOUNT_URL`, `CANOPY_APNS_URL`, `CAPTION_LANG`, `SKIP_CATEGORIES`, `SKIP_ADS`, `MUTE_ADS`, `AUTO_PLAY`, `SKIP_MINIMUM_LENGTH`, `SKIP_JOIN_NAME`, `SKIP_COUNT_TRACKING`
   - Remove any `ISPONSORBLOCKTV_CONFIG` there: the compose file sets it.
4. **Persistent Storage**: nothing to add. Coolify creates the compose file's volumes (`api-data` for `/data`, `isbtv-config` shared). This is a fresh start: everyone re-pairs their TVs. The Dockerfile-mode storage for `/data` isn't used any more; delete it once the new deployment works.
5. **Deploy**. In the logs: `api` says `skip sponsors on: iSponsorBlockTV config at /isbtv/config.json`; `isponsorblocktv` says `[wrapper] no devices in config.json` until someone turns skip sponsors on, then `starting iSponsorBlockTV for 1 device(s)`.
6. Install the Renovate GitHub app on `narenh/canopy-control` (and, optionally, make `CI` a required check on `main`).

## Open items
- Multi-TV support not yet tested against real TVs (single-TV pairing, speed and captions confirmed working).
- If YouTube resets speed on the next video, captions stay on until the slider moves; consider re-applying on `now_playing_changed`.
