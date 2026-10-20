"""iSponsorBlockTV's config.json, rendered by this server for the isponsorblocktv container.

The container (docker-compose.yml) runs the upstream image unmodified, under a small wrapper that
restarts it whenever config.json changes: iSponsorBlockTV reads its config only at startup. This
server owns the file (the shared volume is read-only over there) and writes it atomically, and only
when its content changes.

    cfg = from_env(os.environ)        # None when ISPONSORBLOCKTV_CONFIG isn't set: feature off
    cfg.write([(screen_id, name), ...])   # True if the file was rewritten

Settings, in iSponsorBlockTV's own key names (its src/iSponsorBlockTV/helpers.py Config):
SKIP_CATEGORIES -> skip_categories (comma-separated; default sponsor,selfpromo,music_offtopic),
SKIP_ADS -> skip_ads (default true), MUTE_ADS -> mute_ads (false), AUTO_PLAY -> auto_play (false),
SKIP_MINIMUM_LENGTH -> minimum_skip_length (seconds, default 1), SKIP_JOIN_NAME -> join_name (the
name shown on the TV, default "Canopy Control"), SKIP_COUNT_TRACKING -> skip_count_tracking
(default true). apikey is "" and channel_whitelist is [] (a whitelist needs a YouTube API key).
SKIP_HIDDEN_PERSON_IDS: comma-separated Canopy person ids who never see or get the feature.
"""
import json, logging, os, tempfile
from pathlib import Path

log = logging.getLogger("isbtv")
# iSponsorBlockTV's categories (src/iSponsorBlockTV/constants.py); others are kept, with a warning
KNOWN_CATEGORIES = {"sponsor", "selfpromo", "intro", "outro", "music_offtopic", "interaction",
                    "exclusive_access", "preview", "filler", "hook"}
DEFAULT_CATEGORIES = "sponsor,selfpromo,music_offtopic"
DEFAULT_JOIN_NAME = "Canopy Control"
TRUE, FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off")


def _list(raw: str | None) -> list[str]:
    seen = []
    for x in (raw or "").split(","):
        x = x.strip()
        if x and x not in seen:
            seen.append(x)
    return seen


def _bool(env, key: str, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in TRUE:
        return True
    if raw in FALSE:
        return False
    raise ValueError(f"{key} must be true or false, not {env.get(key)!r}")


def _number(env, key: str, default: float) -> int | float:
    raw = (env.get(key) or "").strip()
    try:
        n = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"{key} must be a number of seconds, not {raw!r}") from None
    if not 0 <= n < 3600:
        raise ValueError(f"{key} must be 0 or more seconds, under an hour")
    return int(n) if n == int(n) else n


class Config:
    def __init__(self, path: Path, hidden: set[str], settings: dict):
        self.path = path
        self.hidden = hidden
        self.settings = settings  # everything but devices

    @staticmethod
    def devices(tvs) -> list[dict]:
        """iSponsorBlockTV's devices from (screen_id, name) pairs: one per screen id, first name wins."""
        out = {}
        for screen_id, name in tvs:
            if screen_id and screen_id not in out:
                out[screen_id] = {"screen_id": screen_id, "name": name, "offset": 0}
        return list(out.values())

    def render(self, tvs) -> str:
        return json.dumps({"devices": self.devices(tvs), **self.settings}, indent=4, ensure_ascii=False) + "\n"

    def write(self, tvs) -> bool:
        """Write config.json if its content would change. Atomic: a temp file in the same
        directory, then os.replace, so the reader never sees half a file."""
        text = self.render(tvs)
        try:
            if self.path.read_text(encoding="utf-8") == text:
                return False
        except (FileNotFoundError, UnicodeDecodeError):
            pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            # readable by the isponsorblocktv container's user (same uid/gid), not by others
            os.chmod(tmp, 0o640)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        return True


def from_env(env) -> Config | None:
    """The config from the environment, or None if ISPONSORBLOCKTV_CONFIG isn't set. Raises
    ValueError for a setting that can't be right (so the server doesn't start with it)."""
    path = (env.get("ISPONSORBLOCKTV_CONFIG") or "").strip()
    if not path:
        return None
    categories = _list(env.get("SKIP_CATEGORIES") if (env.get("SKIP_CATEGORIES") or "").strip()
                       else DEFAULT_CATEGORIES)
    unknown = [c for c in categories if c not in KNOWN_CATEGORIES]
    if unknown:
        log.warning("SKIP_CATEGORIES has categories iSponsorBlockTV may not know: %s", ", ".join(unknown))
    settings = {
        "skip_categories": categories,
        "skip_count_tracking": _bool(env, "SKIP_COUNT_TRACKING", True),
        "mute_ads": _bool(env, "MUTE_ADS", False),
        "skip_ads": _bool(env, "SKIP_ADS", True),
        "minimum_skip_length": _number(env, "SKIP_MINIMUM_LENGTH", 1),
        "auto_play": _bool(env, "AUTO_PLAY", False),
        "join_name": (env.get("SKIP_JOIN_NAME") or "").strip() or DEFAULT_JOIN_NAME,
        "apikey": "",
        "channel_whitelist": [],
        "use_proxy": False,
    }
    return Config(Path(path), set(_list(env.get("SKIP_HIDDEN_PERSON_IDS"))), settings)
