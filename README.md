# yt-speedcontrol

Self-hosted playback-speed remote for YouTube on a TV, for the Canopy iOS app. Uses the unofficial YouTube Lounge API (via `pyytlounge`) to hold sessions to paired TVs.

## Sign-in and per-person TVs

Every route needs a Canopy Account: the app sends `Authorization: Bearer <token>` (its Canopy session token), and the server checks it with the account service (`GET /api/session`, cached 60 s; a cached answer up to 15 min old stands in if the account service is down). People whose email isn't proven aren't let in.

Each person sees and controls only the TVs they paired. Two people can pair the same TV; each gets their own Lounge session. Up to 10 TVs per person. Another person's TV, or an unknown id, is a 404.

There is no web UI any more. `/docs` and `/openapi.json` are FastAPI's.

## API

- `GET /devices` — your TVs: `[{"id", "name", "connected", "video_id", "speed", "captions"}]`
- `POST /pair {"code": "..."}` — add a TV (YouTube → Settings → Link with TV code); returns that TV. 400 `pairing failed`; 409 `too_many_devices` past 10. Re-pairing a TV you already have replaces it.
- `DELETE /devices/{id}` — forget a TV: `{"removed": id}`
- `POST /devices/{id}/speed/{x}` — x in 1, 1.25, 1.5, 1.75, 2; captions on above 1x, off at 1x (`CAPTION_LANG`, default `en`)
- `POST /devices/{id}/captions/{on|off}` — manual caption toggle; 409 if the TV does not confirm the change. Whichever of slider/toggle was used last wins and is re-applied to each new video

Errors are `{"detail": "<sentence>"}`; the auth and limit ones add `"reason"`:

| status | reason | when |
|---|---|---|
| 401 | `signed_out` | no token, a malformed one, or signed out |
| 403 | `email_unverified` | the account's email isn't proven |
| 503 | `account_unreachable` | the account service can't be reached and nothing recent is cached |
| 409 | `too_many_devices` | pairing an 11th TV |

Others (no `reason`): 404 `unknown device`, 503 `<TV> not connected (is YouTube open on it?)`, 409 `no video playing`, 409 `<TV> didn't turn captions on/off…`, 400 for a bad speed, mode or pairing code.

## Setup

The account admin makes a site for this server in the Account Manager's **Sites** tab: no contact fields granted, **Allows quick (unverified) accounts** off. Its key goes in Coolify as `CANOPY_ACCOUNT_KEY`.

Environment:

- `CANOPY_ACCOUNT_KEY` — required. Without it the server refuses to start (it never runs unauthenticated).
- `CANOPY_ACCOUNT_URL` — default `https://account.canopysf.com`
- `CAPTION_LANG` — default `en`
- `DATA_DIR` — default `/data`

```
CANOPY_ACCOUNT_KEY=cnp_... docker compose up -d --build
```

Pairings persist in `./data/devices.json`, each with its owner's Canopy person id. Entries from before accounts (no owner), and an old single-TV `auth.json`, are dropped at startup with a warning: re-pair those TVs from the app.

## Open items
- Multi-TV support not yet tested against real TVs (single-TV pairing, speed and captions confirmed working).
- If YouTube resets speed on the next video, captions stay on until the slider moves; consider re-applying on `now_playing_changed`.
