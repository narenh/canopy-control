"""Playing again after a pause counts as starting to play (so a missing Live Activity comes back).

    python -I tests/test_resume.py [repo]

Only the decision in Device._note_state: live.started_playing is recorded, not run (it decides
update vs start and rate-limits starts; test_push.py covers that).
"""
import os, sys, tempfile, types
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(REPO))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("CANOPY_ACCOUNT_KEY", "cnp_test")
import main  # noqa: E402
from pyytlounge.models import State  # noqa: E402

passed = 0


def check(name, cond, info=""):
    global passed
    assert cond, f"{name}: {info}"
    passed += 1


calls = []
main.live.started_playing = lambda owner, tv: calls.append(tv)
main.live.ended = lambda *a, **k: None  # no pushes: the end rule isn't under test here

d = main.Device("owner-1", "TV")
d.api = types.SimpleNamespace(auth=types.SimpleNamespace(screen_id="scr-1"), connected=lambda: True)
d._stopping = lambda why: None
d._playing_again = lambda *a, **k: None
d.video_id = "abcdefghijk"


def state(s, video=True):
    before = len(calls)
    d._note_state(s, video)
    return len(calls) > before


check("first Playing of the session starts", state(State.Playing))
check("Playing again (no change) doesn't", not state(State.Playing))
check("Paused doesn't", not state(State.Paused))
check("Playing after Paused counts as starting (resumed)", state(State.Playing))
check("and only once", not state(State.Playing))
state(State.Paused)
check("Starting (buffering) after Paused counts too", state(State.Starting))
check("then Playing doesn't count again", not state(State.Playing))
state(State.Paused)
state(State.Paused)
check("several Paused reports, then Playing: one start", state(State.Playing) and not state(State.Playing))
state(State.Paused)
state(State.Buffering)  # ended
d._stopped_at = main.live.clock()  # just now: not the 30-minute idle rule
check("Paused then ended then Playing (replay): not a resume", not state(State.Playing))
state(State.Paused, video=False)
check("a no-video report isn't a pause", not state(State.Playing))

print(f"{passed} checks passed")
