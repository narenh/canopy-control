"""The isponsorblocktv service's wrapper (its entrypoint script in docker-compose.yml), run locally.

python -I tests/test_wrapper.py [repo path, default: this repo]

Runs the script exactly as the compose file has it, with a fake iSponsorBlockTV as the child and a
temp data dir: idle without devices, start once config.json lists one, restart on a change
(including an atomic replace), restart after a crash, and SIGTERM forwarded on stop.
"""
import json, os, signal, subprocess, sys, tempfile, threading, time
from pathlib import Path

import yaml

REPO = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent)  # default: this repo
compose = yaml.safe_load((Path(REPO) / "docker-compose.yml").read_text())
svc = compose["services"]["isponsorblocktv"]
assert svc["entrypoint"][:2] == ["python3", "-c"], svc["entrypoint"]
SCRIPT = svc["entrypoint"][2]
assert "$" not in SCRIPT, "compose would interpolate $ in the wrapper"

DATA = Path(tempfile.mkdtemp())
CONF = DATA / "config.json"
# the fake child: says it started (with the device count it reads), exits 0 on SIGTERM, or crashes on request
CHILD = """
import json, signal, sys, time
n = len(json.load(open(sys.argv[1]))["devices"])
print(f"child up devices={n}", flush=True)
def bye(*_):
    print("child got SIGTERM", flush=True); sys.exit(0)
signal.signal(signal.SIGTERM, bye)
while True:
    if open(sys.argv[1]).read().find("crash-me") >= 0 and n == 2:
        print("child crashing", flush=True); sys.exit(3)
    time.sleep(0.05)
"""
passed, lines = [], []


def check(name, cond, info=""):
    assert cond, f"{name}: {info}\n--- output:\n" + "".join(lines[-40:])
    passed.append(name)


def write(devices, extra=None, atomic=True):
    text = json.dumps({"devices": [{"screen_id": d, "name": d, "offset": 0} for d in devices], **(extra or {})})
    if atomic:
        tmp = DATA / ".config.tmp"
        tmp.write_text(text)
        os.replace(tmp, CONF)
    else:
        CONF.write_text(text)


def wait_for(text, after=0, s=8):
    end = time.time() + s
    while time.time() < end:
        if any(text in l for l in lines[after:]):
            return True
        time.sleep(0.05)
    return False


def count(text): return sum(text in l for l in lines)


env = dict(os.environ, iSPBTV_data_dir=str(DATA), WRAPPER_POLL_S="0.2")
child = Path(tempfile.mkdtemp()) / "fake_isbtv.py"
child.write_text(CHILD)
p = subprocess.Popen([sys.executable, "-c", SCRIPT, sys.executable, "-I", str(child), str(CONF)], env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
threading.Thread(target=lambda: [lines.append(l) for l in p.stdout], daemon=True).start()

check("logs what it watches", wait_for("[wrapper] watching " + str(CONF)))
check("no config: idles", wait_for("no devices in config.json"))
time.sleep(0.5); check("no config: no child", count("child up") == 0)
write([])
check("empty devices: still idle", wait_for("config.json changed") and wait_for("no devices", after=2))
time.sleep(0.5); check("empty devices: no child", count("child up") == 0)

n = len(lines); write(["tv1"])
check("a device: starts the child", wait_for("child up devices=1", n), lines[n:])
time.sleep(1); check("unchanged: no restart", count("child up") == 1 and count("child got SIGTERM") == 0)

n = len(lines); write(["tv1", "tv2"])
check("atomic replace: child stopped with SIGTERM", wait_for("child got SIGTERM", n))
check("and started again with the new config", wait_for("child up devices=2", n))

n = len(lines); write(["tv1", "tv2"], {"x": 1}, atomic=False)
check("in-place write also restarts", wait_for("child got SIGTERM", n) and wait_for("child up devices=2", n))

n = len(lines); write(["tv1", "tv2"], {"x": "crash-me"})
check("crash: exit code logged with a backoff", wait_for("exited with code 3: restarting in 5 s", n), lines[n:])
ups, t0 = count("child up"), time.time()
end = time.time() + 10
while count("child up") == ups and time.time() < end:
    time.sleep(0.05)
check("crash: restarted after the backoff", count("child up") == ups + 1 and 4.5 < time.time() - t0 < 7, lines[n:])

n = len(lines); write([])
check("devices gone: idle, no restart", wait_for("no devices", n))
time.sleep(6); check("idle: the pending crash restart is dropped", count("child up") == ups + 1)

n = len(lines); write(["tv3"])
check("devices back: started", wait_for("child up devices=1", n))
t0 = time.time(); p.send_signal(signal.SIGTERM)
code = p.wait(10)
check("SIGTERM: forwarded to the child", wait_for("child got SIGTERM", n + 1, s=2))
check("SIGTERM: wrapper exits 0, promptly", code == 0 and time.time() - t0 < 3, (code, time.time() - t0))
check("SIGTERM: logs stopped", wait_for("[wrapper] stopped", n, s=2))

print(f"{len(passed)} checks passed")
