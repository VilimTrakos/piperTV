#!/usr/bin/env bash
# Deploy a git commit of PiperTV to the Raspberry Pi and restart it there.
#
#   ./deploy.sh                 deploy HEAD (refuses a dirty tree)
#   ./deploy.sh --ref f6c361a   deploy another commit, e.g. to roll back
#   ./deploy.sh --dirty         deploy the working tree as it is
#   ./deploy.sh --no-kiosk      don't restart the interface on the TV
#   ./deploy.sh --kiosk         restart it even if no page changed
#   ./deploy.sh --no-session    don't open a control session afterwards
#   ./deploy.sh --force         deploy even while a service is open on the TV
#   ./deploy.sh --skip-tests    don't run the test suite first
#
# The SSH password is asked once (one multiplexed connection) or read from
# PIPER_PW. PIPER_HOST, PIPER_USER and PIPER_DIR say where the Pi is.
set -euo pipefail

HOST=${PIPER_HOST:-192.168.1.108}
LOGIN=${PIPER_USER:-rpi}
DIR=${PIPER_DIR:-/home/rpi/piperTV}
PORT=${PIPER_PORT:-8765}
PROFILE=${PIPER_KIOSK_PROFILE:-/tmp/kiosk-gpu-off}
REF=HEAD; DIRTY=0; KIOSK=""; TESTS=1; SESSION=1; FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF=${2:?--ref needs a commit}; shift 2 ;;
    --dirty) DIRTY=1; shift ;;
    --no-kiosk) KIOSK=0; shift ;;
    --kiosk) KIOSK=1; shift ;;
    --force) FORCE=1; shift ;;
    --no-session) SESSION=0; shift ;;
    --skip-tests) TESTS=0; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "deploy.sh: unknown option $1" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")"
say() { printf '\n== %s\n' "$*"; }

# pkill -f would also match the remote shell's own command line, so patterns
# are sent as [m]ain.py, and stopping and starting use separate connections.
bracket() { printf '[%s]%s' "${1:0:1}" "${1:1}"; }
APP_PATTERN=$(bracket "main.py")
KIOSK_PATTERN=$(bracket "$(basename "$PROFILE")")

# --- what to send -----------------------------------------------------------

if [ "$DIRTY" = 1 ]; then
  SOURCE=.
  VERSION="working tree$(git diff --quiet && git diff --cached --quiet || echo ' (uncommitted)')"
else
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "deploy.sh: the working tree has uncommitted changes." >&2
    echo "           Commit them, or use --dirty to send them anyway." >&2
    exit 1
  fi
  VERSION="$(git rev-parse --short "$REF") $(git log -1 --format=%s "$REF")"
fi

if [ "$TESTS" = 1 ]; then
  say "Tests"
  ./.venv/bin/python3 -m unittest discover -s tests -t . 2>&1 | tail -3
fi

STAGE=$(mktemp -d /tmp/pipertv-deploy-XXXXXX)
CONTROL=$(mktemp -u /tmp/pipertv-ssh-XXXXXX)
cleanup() {
  ssh -S "$CONTROL" -O exit "$LOGIN@$HOST" 2>/dev/null || true
  rm -rf "$STAGE"
}
trap cleanup EXIT

if [ "$DIRTY" = 1 ]; then
  rsync -a --exclude=__pycache__ pipertv main.py requirements.txt install.sh "$STAGE/"
else
  # Older commits have no install.sh.
  EXTRA=(); git cat-file -e "$REF:install.sh" 2>/dev/null && EXTRA=(install.sh)
  git archive "$REF" pipertv main.py requirements.txt "${EXTRA[@]}" | tar -x -C "$STAGE"
fi

# --- one connection, one password -------------------------------------------

say "Connecting to $LOGIN@$HOST"
if [ -n "${PIPER_PW:-}" ]; then
  # Answer the password prompt on a pty, so the password never ends up in a
  # file, an argument list or the shell history.
  PIPER_PW="$PIPER_PW" python3 - "$CONTROL" "$LOGIN@$HOST" <<'PY'
import os, pty, select, sys
control, target = sys.argv[1], sys.argv[2]
pid, fd = pty.fork()
if pid == 0:
    os.execvp("ssh", ["ssh", "-M", "-S", control, "-o", "ControlPersist=600",
                      "-o", "StrictHostKeyChecking=accept-new", "-fN", target])
sent, tail = False, b""
while True:
    ready, _, _ = select.select([fd], [], [], 60)
    if not ready:
        break
    try:
        chunk = os.read(fd, 1024)
    except OSError:
        break
    if not chunk:
        break
    tail = (tail + chunk)[-200:]
    if not sent and b"assword" in tail:
        os.write(fd, os.environ["PIPER_PW"].encode() + b"\n")
        sent = True
os.waitpid(pid, 0)
PY
else
  ssh -M -S "$CONTROL" -o ControlPersist=600 -o StrictHostKeyChecking=accept-new -fN "$LOGIN@$HOST"
fi
ssh -S "$CONTROL" -O check "$LOGIN@$HOST" >/dev/null 2>&1 || { echo "deploy.sh: could not connect." >&2; exit 1; }
pi() { ssh -S "$CONTROL" -o BatchMode=yes -n "$LOGIN@$HOST" "$@"; }

# --- send it ----------------------------------------------------------------

# Don't interrupt someone who is watching something.
WATCHING=$(curl -s -m 5 "http://$HOST:$PORT/api/tv/events?after=0" 2>/dev/null \
  | python3 -c "import json,sys
try: running = (json.load(sys.stdin).get('services') or {}).get('running')
except Exception: running = None
print(running['name'] if running else '')" 2>/dev/null || echo "")
if [ -n "$WATCHING" ] && [ "$FORCE" = 0 ]; then
  echo "deploy.sh: $WATCHING is open on the television right now." >&2
  echo "           Deploying would close it and blank the screen. Wait, or use --force." >&2
  exit 1
fi

# Hash the pages before copying, to tell afterwards whether they changed.
PAGES="find pipertv/static -type f | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1"
WAS_SHOWING=$(pi "cd $DIR && $PAGES" 2>/dev/null || echo "")

say "Sending $VERSION"
rsync -az --delete --exclude=__pycache__ -e "ssh -S $CONTROL -o BatchMode=yes" \
  "$STAGE/pipertv/" "$LOGIN@$HOST:$DIR/pipertv/"
ROOT_FILES=("$STAGE/main.py" "$STAGE/requirements.txt")
[ -f "$STAGE/install.sh" ] && ROOT_FILES+=("$STAGE/install.sh")
rsync -az -e "ssh -S $CONTROL -o BatchMode=yes" "${ROOT_FILES[@]}" "$LOGIN@$HOST:$DIR/"

# LC_ALL=C on both sides, or the two machines sort the paths differently.
SUMS="find pipertv \\( -name '*.py' -o -name '*.js' -o -name '*.css' -o -name '*.html' \\) | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1"
HERE=$(cd "$STAGE" && eval "$SUMS")
THERE=$(pi "cd $DIR && $SUMS")
[ "$HERE" = "$THERE" ] || { echo "deploy.sh: what arrived does not match what was sent." >&2; exit 1; }
echo "checksum matches: ${HERE:0:16}"

# --- stop, then start (separate connections: see bracket() above) ------------

if [ -z "$KIOSK" ]; then
  # Restart the interface only if its page changed or it isn't showing; a
  # Python-only change just restarts the app and the screen stays as it is.
  SHOWING=$(pi "ps -eo args | grep -c '[c]hromium --type=renderer' || true")
  if [ "${SHOWING:-0}" -lt 1 ]; then
    KIOSK=1
    echo "the interface is not on the screen; starting it"
  elif [ "$(cd "$STAGE" && eval "$PAGES")" = "$WAS_SHOWING" ]; then
    KIOSK=0
  else
    KIOSK=1
    echo "the interface page changed; restarting it"
  fi
fi

say "Stopping the old app and interface"
if [ "$KIOSK" = 1 ]; then
  pi "pkill -f '$APP_PATTERN' || true; pkill -f '$KIOSK_PATTERN' || true; sleep 2; echo 'app and interface stopped'"
else
  pi "pkill -f '$APP_PATTERN' || true; sleep 2; echo 'app stopped; the interface was left where it was'"
fi

# Started over SSH the app has no desktop session; point it at the Wayland display.
WAYLAND_ENV='export XDG_RUNTIME_DIR=/run/user/$(id -u); export WAYLAND_DISPLAY=$(ls "$XDG_RUNTIME_DIR" | grep -m1 "^wayland-[0-9]$")'

say "Starting the app"
# The SSH channel can stay open while the app holds it, hence the timeout; the
# health check below is what counts.
timeout 25 ssh -S "$CONTROL" -o BatchMode=yes -n "$LOGIN@$HOST" \
  "$WAYLAND_ENV; cd $DIR && setsid nohup ./.venv/bin/python3 main.py >> pipertv.log 2>&1 < /dev/null & disown; exit 0" || true
sleep 4

if [ "$KIOSK" = 1 ]; then
  say "Starting the interface on the TV"
  # The app knows the window settings, so let it open the interface.
  pi "curl -s -m 45 -X POST -H 'Content-Type: application/json' -d '{}' \
       http://127.0.0.1:$PORT/api/tv/interface" \
    | python3 -c "import json,sys
try: shown = json.load(sys.stdin)
except Exception: shown = {}
print('interface:', 'up' if shown.get('showing') else shown.get('error') or 'no answer')"
fi

# --- prove it ---------------------------------------------------------------

say "Checking"
pi "curl -s -m 5 http://127.0.0.1:$PORT/api/health > /tmp/pipertv-health.json && echo 'app: UP' || { echo 'app: DOWN'; tail -15 $DIR/pipertv.log; exit 1; }
python3 - <<'PY'
import json
health = json.load(open('/tmp/pipertv-health.json'))
control = health.get('control') or {}
services = control.get('services')
receiver = control.get('receiver') or {}
print('receiver:', receiver.get('learned_buttons'), 'buttons, error:', receiver.get('error'))
if services is None:
    print('services: this version cannot open anything on the TV')
else:
    print('services:', 'ready' if services['available'] else services['reason'],
          '| browser:', services.get('browser'))
print('detection:', (control.get('detection') or {}).get('state'))
PY
for _ in \$(seq 20); do
  WINDOWS=\$(ps -eo args | grep -c \"[c]hromium --type=renderer\")
  [ \"\$WINDOWS\" -ge 1 ] && break
  sleep 1                     # a renderer takes a few seconds on a 3B+
done
echo \"interface windows: \$WINDOWS\"
[ \"\$WINDOWS\" -ge 1 ] || { echo 'the interface did not come up; last lines of its log:'; tail -5 /tmp/kiosk.log; exit 1; }"

if [ "$SESSION" = 1 ]; then
  say "Opening a Piper session"
  # This TV never reports its input over CEC, so after a restart the remote does
  # nothing until someone confirms the TV shows the Pi. Deploying counts as that
  # confirmation (recorded as manual).
  ID=$(curl -s -m 5 -X POST -H 'Content-Type: application/json' -d '{"confirmed":true}' \
        "http://$HOST:$PORT/api/control/manual" \
       | python3 -c "import json,sys; print((json.load(sys.stdin).get('session') or {}).get('id',''))")
  if [ -z "$ID" ]; then
    echo "could not open a session; the remote will do nothing but leave" >&2
  else
    curl -s -m 5 -X POST -H 'Content-Type: application/json' \
      -d "{\"mode\":\"piper\",\"session_id\":\"$ID\"}" "http://$HOST:$PORT/api/control/mode" \
      | python3 -c "import json,sys; d=json.load(sys.stdin); print('control:', d['control'], '| mode:', d['mode'], '| origin:', d['origin'])"
  fi
fi

say "Done: $VERSION"
