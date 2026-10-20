# yt-speedcontrol

Self-hosted playback-speed remote for YouTube on a TV. Uses the unofficial YouTube Lounge API (via `pyytlounge`) to hold sessions to one or more paired TVs.

- `/` — web UI: TV switcher at the top, Pair button top right, slider 1x / 1.25 / 1.5 / 1.75 / 2x, captions checkbox
- `GET /devices` — paired TVs with connection and playback state
- `POST /pair {"code": "..."}` — add a TV (YouTube → Settings → Link with TV code)
- `DELETE /devices/{id}` — forget a TV
- `POST /devices/{id}/speed/{x}` — sets speed; captions on above 1x, off at 1x (`CAPTION_LANG`, default `en`)
- `POST /devices/{id}/captions/{on|off}` — manual caption toggle

```
docker compose up -d --build
```

Pairings persist in `./data/devices.json` (an older single-TV `auth.json` is migrated automatically). No auth on the HTTP API — LAN only.

## Open items
- Multi-TV support not yet tested against real TVs (single-TV pairing, speed and captions confirmed working).
- If YouTube resets speed on the next video, captions stay on until the slider moves; consider re-applying on `now_playing_changed`.
- Native iOS client later.
- Later (own branch): Canopy accounts integration — sign in and link multiple TVs per user. See the canopy-account repo for account details.
