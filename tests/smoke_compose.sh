#!/bin/sh
# The compose app for real, with the upstream iSponsorBlockTV image (CI runs this, so it's what a
# Renovate update of that image has to pass before it auto-merges). No real TV or account needed:
#   - api starts with a dummy CANOPY_ACCOUNT_KEY and answers 401 on /devices
#   - isponsorblocktv runs as 10001 with a read-only root fs and config mount, no capabilities,
#     no-new-privileges; with no devices the wrapper doesn't start iSponsorBlockTV
#   - api's rendering code writes a (dummy) device: iSponsorBlockTV starts and keeps running
#   - another change restarts it; stopping the container stops it cleanly
# Usage: tests/smoke_compose.sh   (needs docker compose; leaves nothing behind)
set -eu
cd "$(dirname "$0")/.."
export CANOPY_ACCOUNT_KEY=cnp_dummy_smoke_test
P=canopy-smoke-$$
dc() { docker compose -p "$P" "$@"; }
fail() { echo "FAIL: $*"; dc ps -a; dc logs --no-color --tail 80; exit 1; }
ok() { echo "ok: $*"; }
trap 'dc down -v --remove-orphans >/dev/null 2>&1 || true' EXIT

dc up -d --build
ISB=$(dc ps -q isponsorblocktv)
API=$(dc ps -q api)

wait_log() {  # wait_log <service> <text> [seconds]
  i=0; while [ "$i" -lt "${3:-30}" ]; do
    dc logs --no-color "$1" 2>&1 | grep -qF -- "$2" && return 0
    i=$((i + 1)); sleep 1
  done; return 1
}
count_log() { dc logs --no-color "$1" 2>&1 | grep -cF -- "$2" || true; }

# --- api
i=0; code=""
while [ "$i" -lt 30 ]; do
  code=$(dc exec -T api python -c "
import urllib.request, urllib.error
try: urllib.request.urlopen('http://127.0.0.1:8000/devices')
except urllib.error.HTTPError as e: print(e.code)
" 2>/dev/null || true)
  [ "$code" = 401 ] && break; i=$((i + 1)); sleep 1
done
[ "$code" = 401 ] || fail "api /devices answered '$code', not 401"
ok "api answers 401 on /devices without a token"
wait_log api "skip sponsors on: iSponsorBlockTV config at /isbtv/config.json" || fail "api didn't turn skip sponsors on"
ok "api has skip sponsors on"

# --- hardening, as Docker applied it
for c in "$API" "$ISB"; do
  name=$(docker inspect -f '{{.Name}}' "$c")
  [ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$c")" = true ] || fail "$name: root fs not read-only"
  [ "$(docker inspect -f '{{.HostConfig.CapDrop}}' "$c")" = "[ALL]" ] || fail "$name: capabilities not dropped"
  docker inspect -f '{{.HostConfig.SecurityOpt}}' "$c" | grep -q "no-new-privileges:true" || fail "$name: no no-new-privileges"
  [ "$(docker inspect -f '{{.Config.User}}' "$c")" = "10001:10001" ] || fail "$name: not user 10001"
  [ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c")" = unless-stopped ] || fail "$name: restart policy"
  [ "$(docker inspect -f '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}{{end}}{{end}}' "$c")" = "" ] || fail "$name: has a bind mount"
done
docker inspect -f '{{range .Mounts}}{{.Destination}}={{.RW}} {{end}}' "$ISB" | grep -q "/app/data=false" || fail "config mount isn't read-only"
ok "both: uid 10001, read-only root fs, cap_drop ALL, no-new-privileges, unless-stopped, no bind mounts (no Docker socket)"
[ "$(dc exec -T isponsorblocktv id -u)" = 10001 ] || fail "isponsorblocktv not running as 10001"
dc exec -T isponsorblocktv sh -c 'touch /app/data/x' 2>/dev/null && fail "config dir is writable from isponsorblocktv"
dc exec -T isponsorblocktv sh -c 'touch /app/x' 2>/dev/null && fail "root fs is writable in isponsorblocktv"
dc exec -T isponsorblocktv sh -c 'touch /tmp/x' || fail "/tmp isn't writable in isponsorblocktv"
ok "inside isponsorblocktv: uid 10001; /app/data and / read-only; /tmp writable"

# --- no devices: idle
wait_log isponsorblocktv "no devices in config.json" || fail "wrapper didn't report no devices"
[ "$(count_log isponsorblocktv 'starting iSponsorBlockTV')" = 0 ] || fail "started iSponsorBlockTV with no devices"
ok "no devices: the wrapper idles without starting iSponsorBlockTV"

# --- a device (api's own rendering code, as uid 10001, atomic): starts and stays up
render() {
  dc exec -T api python -c "
import os, isbtv
print(isbtv.from_env(os.environ).write([('smoke0test0screen0id0not0a0real0tv0aaaaaaaaaa', '$1')]))"
}
[ "$(render 'Smoke TV')" = True ] || fail "api couldn't write the config"
dc exec -T isponsorblocktv cat /app/data/config.json | grep -q '"Smoke TV"' || fail "isponsorblocktv can't read the config"
wait_log isponsorblocktv "starting iSponsorBlockTV for 1 device(s)" 15 || fail "wrapper didn't start iSponsorBlockTV"
sleep 15
dc logs --no-color isponsorblocktv 2>&1 | grep -qE "exited with code|Traceback|Read-only|Permission denied" && fail "iSponsorBlockTV failed"
ok "a device: iSponsorBlockTV started and is still running after 15 s"

# --- a change restarts it
[ "$(render 'Smoke TV 2')" = True ] || fail "api couldn't rewrite the config"
[ "$(render 'Smoke TV 2')" = False ] || fail "unchanged config was rewritten"
wait_log isponsorblocktv "config.json changed" 15 || fail "wrapper didn't see the change"
i=0; while [ "$(count_log isponsorblocktv 'starting iSponsorBlockTV')" -lt 2 ] && [ "$i" -lt 20 ]; do i=$((i + 1)); sleep 1; done
[ "$(count_log isponsorblocktv 'starting iSponsorBlockTV')" = 2 ] || fail "iSponsorBlockTV wasn't restarted"
dc logs --no-color isponsorblocktv 2>&1 | grep -q "didn't stop in" && fail "iSponsorBlockTV needed a SIGKILL"
ok "a config change restarts iSponsorBlockTV (it stopped on SIGTERM)"

# --- a clean stop
t0=$(date +%s)
dc stop -t 20 isponsorblocktv >/dev/null 2>&1
t=$(( $(date +%s) - t0 ))
[ "$(docker inspect -f '{{.State.ExitCode}}' "$ISB")" = 0 ] || fail "isponsorblocktv exited $(docker inspect -f '{{.State.ExitCode}}' "$ISB")"
[ "$t" -lt 15 ] || fail "stopping took ${t} s"
wait_log isponsorblocktv "[wrapper] stopped" 2 || fail "wrapper didn't log its stop"
ok "container stop: SIGTERM forwarded, exit 0 in ${t} s"
echo "--- isponsorblocktv log"; dc logs --no-color isponsorblocktv 2>&1 | tail -40
echo "smoke test passed"
