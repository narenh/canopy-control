# yt-speedcontrol

Self-hosted playback-speed remote for YouTube on a TV, for the Canopy iOS app. Uses the unofficial YouTube Lounge API (via `pyytlounge`) to hold sessions to paired TVs.

## Sign-in and per-person TVs

Every route needs a Canopy Account: the app sends `Authorization: Bearer <token>` (its Canopy session token), and the server checks it with the account service (`GET /api/session`, cached 60 s; a cached answer up to 15 min old stands in if the account service is down). People whose email isn't proven aren't let in.

Each person sees and controls only the TVs they paired. Two people can pair the same TV; each gets their own Lounge session. Up to 10 TVs per person. Another person's TV, or an unknown id, is a 404.

There is no web UI any more. `/docs` and `/openapi.json` are FastAPI's.

## API

- `GET /devices` — your TVs: `[{"id", "name", "connected", "video_id", "speed", "captions"}]`
- `POST /pair {"code": "..."}` — add a TV (YouTube → Settings → Link with TV code); returns that TV. 400 `pairing failed`; 409 `too_many_devices` past 10. Re-pairing a TV you already have replaces it.
- `PATCH /devices/{id} {"name": "..."}` — rename a TV; returns that TV. The name is trimmed and must be 1–40 characters (400 otherwise); `null` or `""` goes back to the name the TV reports. The custom name is yours only, survives reconnects and re-pairing, and is what `GET /devices` and Live Activities show.
- `DELETE /devices/{id}` — forget a TV: `{"removed": id}`. Live Activities showing it are ended.
- `POST /devices/{id}/speed/{x}` — x in 1, 1.25, 1.5, 1.75, 2; captions on above 1x, off at 1x (`CAPTION_LANG`, default `en`)
- `POST /devices/{id}/captions/{on|off}` — manual caption toggle; 409 if the TV does not confirm the change. Whichever of slider/toggle was used last wins and is re-applied to each new video

Errors are `{"detail": "<sentence>"}`; the auth and limit ones add `"reason"`:

| status | reason | when |
|---|---|---|
| 401 | `signed_out` | no token, a malformed one, or signed out |
| 403 | `email_unverified` | the account's email isn't proven |
| 503 | `account_unreachable` | the account service can't be reached and nothing recent is cached |
| 409 | `too_many_devices` | pairing an 11th TV |

Others (no `reason`): 404 `unknown device`, 404 `unknown install`, 503 `<TV> not connected (is YouTube open on it?)`, 409 `no video playing`, 409 `<TV> didn't turn captions on/off…`, 400 for a bad speed, mode or pairing code.

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
- The relay's `unregistered` clears the token that was used. `failed` is logged and the token kept: `BadDeviceToken` usually means the install's `environment` is wrong.

Content-state, matching the app's Swift type: `{"deviceId", "deviceName", "speed", "captions", "connected"}`. Installs persist in `./data/installs.json`.

Ops, once per server instance:

1. Enroll an instance key for this server on the canopy-apns relay.
2. On the relay, allow it to push Live Activities to the app: `CANOPY_APNS_LIVEACTIVITY_TOPICS=<instance_id>:com.canopysf.CanopyControl`.
3. Set the key as `CANOPY_APNS_KEY` in Coolify and redeploy. Without it the server runs with pushes off (one INFO line at startup) and still stores installs.

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

```
CANOPY_ACCOUNT_KEY=cnp_... docker compose up -d --build
```

Pairings persist in `./data/devices.json`, each with its owner's Canopy person id and any custom name (`custom_name`, next to the TV's own `name`). Entries from before accounts (no owner), and an old single-TV `auth.json`, are dropped at startup with a warning: re-pair those TVs from the app.

## Open items
- Multi-TV support not yet tested against real TVs (single-TV pairing, speed and captions confirmed working).
- If YouTube resets speed on the next video, captions stay on until the slider moves; consider re-applying on `now_playing_changed`.
