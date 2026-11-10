#!/usr/bin/env python3
"""One-time import of Sponsor Block only TVs into skip_tvs.json.

    python scripts/import_skip_tvs.py tvs.json --owner <Canopy person id> --data <DATA_DIR>

tvs.json is a list of {"name": "...", "screen_id": "..."}. They are merged into
<data>/skip_tvs.json (the format main.py's load_skip_tvs reads) for that owner, written atomically.
Idempotent: a TV the owner already has there is left as it is (so a custom name survives); one the
owner already has as a paired TV in devices.json is skipped. Screen ids are printed as their first
8 characters only. It is an admin tool, so the per-person limit (SKIP_TVS_LIMIT / SKIP_TVS_LIMITS, read
from the environment like the server) doesn't stop it, but it warns when the owner ends up over it. Restart the api afterwards, so it reads the file (it loads it at startup).
"""
import argparse, json, os, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # -I leaves the script's directory off
import isbtv  # noqa: E402  (its SKIP_TVS_LIMIT / SKIP_TVS_LIMITS parsing)


def short(screen_id: str) -> str:
    return screen_id[:8] + "..."


def write_atomic(path: Path, text: str):
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


def load_list(path: Path, what: str):
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except Exception as ex:
        sys.exit(f"{what} ({path}) is unreadable, not touching it: {ex}")
    if not isinstance(raw, list):
        sys.exit(f"{what} ({path}) isn't a JSON list, not touching it")
    return raw


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Merge Sponsor Block only TVs into skip_tvs.json")
    ap.add_argument("file", type=Path, help="JSON list of {name, screen_id}")
    ap.add_argument("--owner", required=True, help="Canopy person id the TVs belong to")
    ap.add_argument("--data", required=True, type=Path, help="the api's data directory (DATA_DIR)")
    args = ap.parse_args(argv)
    owner = args.owner.strip()
    if not owner:
        sys.exit("--owner is empty")

    incoming = load_list(args.file, "the input file")
    store = args.data / "skip_tvs.json"
    entries = load_list(store, "skip_tvs.json")
    have = {(e.get("owner"), e.get("screen_id")) for e in entries if isinstance(e, dict)}
    paired = {e.get("auth", {}).get("screenId") for e in load_list(args.data / "devices.json", "devices.json")
              if isinstance(e, dict) and e.get("owner") == owner and isinstance(e.get("auth"), dict)}

    added = same = already_paired = bad = 0
    for item in incoming:
        sid = item.get("screen_id") if isinstance(item, dict) else None
        name = item.get("name") if isinstance(item, dict) else None
        if not (isinstance(sid, str) and sid.strip()):
            bad += 1
            print("  skipped an entry without a screen_id")
            continue
        sid = sid.strip()
        name = name.strip() if isinstance(name, str) and name.strip() else "TV"
        if sid in paired:
            already_paired += 1
            print(f"  {short(sid)} is already a paired TV of this owner: skipped")
        elif (owner, sid) in have:
            same += 1
            print(f"  {short(sid)} already imported")
        else:
            have.add((owner, sid))
            entries.append({"owner": owner, "screen_id": sid, "name": name})
            added += 1
            print(f"  {short(sid)} added as {name!r}")

    if added:
        write_atomic(store, json.dumps(entries, indent=1))
    print(f"{added} added, {same} already there, {already_paired} already paired, {bad} skipped as invalid; "
          f"{sum(1 for e in entries if isinstance(e, dict) and e.get('owner') == owner)} for this owner in {store}")
    total = sum(1 for e in entries if isinstance(e, dict) and e.get("owner") == owner)
    try:
        cfg = isbtv.from_env({"ISPONSORBLOCKTV_CONFIG": "-", **{k: os.environ[k] for k in ("SKIP_TVS_LIMIT", "SKIP_TVS_LIMITS")
                                                                if k in os.environ}})
    except ValueError as ex:
        print(f"warning: can't check the limit: {ex}")
    else:
        if total > cfg.limit_for(owner):
            print(f"warning: this owner now has {total} Sponsor Block only TVs, over their limit of "
                  f"{cfg.limit_for(owner)}; they can't add more until some are removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
