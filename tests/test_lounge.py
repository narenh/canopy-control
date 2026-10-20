"""lounge.LoungeApi: our own remote id, and a guard against pyytlounge upgrades.

    python -I tests/test_lounge.py [repo]

lounge.py copies pyytlounge 3.4.0's YtLoungeApi.connect() and changes only the remote "id". If an
upgrade (Renovate merges them automatically once CI passes) changes upstream's connect(), this
fails, so CI blocks the upgrade until lounge.py is brought in line by hand.
"""
import inspect, re, sys, uuid
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(REPO))

from pyytlounge import YtLoungeApi  # noqa: E402
import lounge  # noqa: E402

passed = 0


def check(name, cond, info=""):
    global passed
    assert cond, f"{name}: {info}"
    passed += 1


def body(fn):
    """A method's source without its docstring and blank lines, dedented."""
    src = inspect.getsource(fn)
    src = re.sub(r'"""[\s\S]*?"""', "", src, count=1)
    return [l.strip() for l in src.splitlines() if l.strip()]


ours, theirs = body(lounge.LoungeApi.connect), body(YtLoungeApi.connect)
check("upstream still sends the screen id as the remote id (the reason for lounge.py)",
      '"id": self.auth.screen_id,' in theirs, theirs)
check("our connect() is upstream's with only the id changed",
      [l for l in ours if l != '"id": self.remote_id,'] == [l for l in theirs if l != '"id": self.auth.screen_id,'],
      "\n".join(set(ours) ^ set(theirs)))


class Listener:
    pass


a1 = lounge.LoungeApi("Speed Remote", Listener(), "person-a")
a1.auth.screen_id = "screen-1"
a2 = lounge.LoungeApi("Speed Remote", Listener(), "person-a")
a2.auth.screen_id = "screen-1"
b = lounge.LoungeApi("Speed Remote", Listener(), "person-b")
b.auth.screen_id = "screen-1"
a_other = lounge.LoungeApi("Speed Remote", Listener(), "person-a")
a_other.auth.screen_id = "screen-2"

check("remote id is a UUID", str(uuid.UUID(a1.remote_id)) == a1.remote_id)
check("stable for the same person and TV (across restarts)", a1.remote_id == a2.remote_id)
check("never the screen id", a1.remote_id != "screen-1")
check("distinct per person on the same TV", a1.remote_id != b.remote_id)
check("distinct per TV for the same person", a1.remote_id != a_other.remote_id)
check("still a YtLoungeApi (everything else unchanged)", isinstance(a1, YtLoungeApi))

print(f"{passed} checks passed")
