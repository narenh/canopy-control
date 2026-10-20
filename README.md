# yt-speedcontrol

Self-hosted playback-speed remote for YouTube on a TV. Uses the unofficial YouTube Lounge API (via `pyytlounge`) to hold a session to one paired TV.

- `/` — one-page web UI: slider 1x / 1.25 / 1.5 / 1.75 / 2x
- `POST /speed/{x}` — sets speed; captions on above 1x, off at 1x (`CAPTION_LANG`, default `en`)
- `POST /pair {"code": "..."}` — pair with YouTube → Settings → Link with TV code
- `GET /status`

```
docker compose up -d --build
```

Auth tokens persist in `./data/auth.json`. No auth on the HTTP API — LAN only.

## Open items
- Not yet tested against a real TV (pairing, setSubtitlesTrack with bare language code).
- If YouTube resets speed on the next video, captions stay on until the slider moves; consider re-applying on `now_playing_changed`.
- Native iOS client later.
