import asyncio, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent))  # default: this repo
import canopy_account as ca
c = ca.CanopyAccount("http://unused", "cnp_x")
calls = []
async def ask(tok): calls.append(tok); return {"person": {"id": tok[:8]}}
c._ask = ask
async def run():
    for i in range(5001):
        await c.who_is(f"{i:043d}")
    assert len(c.cache) == 5001, len(c.cache)
    n = len(c.cache)
    await c.who_is("y" * 43)
    assert len(c.cache) == 1, len(c.cache)  # cleared past 5000, then the new answer
    assert await c.who_is(None) == {"person": None, "unverified": False}
    assert ca.read_token("bearer   " + "a" * 43) == "a" * 43 and ca.read_token("Bearer " + "a" * 42) is None
    print("cache: grew to", n, "then cleared; checks passed")
asyncio.run(run())
