#!/usr/bin/env bash
# Put a version of PiperTV on the Raspberry Pi and restart what runs there.
#
# One command, and the Pi ends up running exactly one app and one kiosk. What
# is deployed is a git commit, not whatever happens to be in the working tree,
# so going back is "./deploy.sh --ref <commit>" rather than an archaeology dig.
#
#   ./deploy.sh                 deploy HEAD (refuses a dirty tree)
#   ./deploy.sh --ref f6c361a   deploy any commit -- this is the way back
#   ./deploy.sh --dirty         deploy the working tree as it stands
#   ./deploy.sh --no-kiosk      leave the browser on the TV alone
#   ./deploy.sh --kiosk         restart it even if no page files changed
#   ./deploy.sh --no-session    do not open a control session afterwards
#   ./deploy.sh --force         deploy even while someone is watching something
#
# Deploying interrupts whoever is at the television: the app stops, which
# closes what it had opened, and the screen is black until the interface comes
# back. So it refuses while a service is open unless --force, and it restarts
# the interface only when the page itself changed.
#   ./deploy.sh --skip-tests    do not run the suite first
#
# The password is asked for once (one multiplexed SSH connection carries every
# step) or taken from PIPER_PW for an unattended run. It is never written down.
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
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "deploy.sh: unknown option $1" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")"
say() { printf '\n== %s\n' "$*"; }

# pkill matches the shell running it, so every pattern goes over as [m]ain.py.
# Nothing else in a remote command may spell the same string out, which is why
# stopping and starting are separate connections.
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
  # install.sh travels with the program since it existed; an older commit,
  # deployed to go back, simply has none.
  EXTRA=(); git cat-file -e "$REF:install.sh" 2>/dev/null && EXTRA=(install.sh)
  git archive "$REF" pipertv main.py requirements.txt "${EXTRA[@]}" | tar -x -C "$STAGE"
fi

# --- one connection, one password -------------------------------------------

say "Connecting to $LOGIN@$HOST"
if [ -n "${PIPER_PW:-}" ]; then
  # Unattended: answer the one prompt on a pty, so the password never reaches
  # a file, an argument list, or the shell history.
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

# Nothing below is worth interrupting a film for.
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

# What the interface is showing has to be asked BEFORE the new files land, or
# the answer is always "the same": the comparison would be the copy against
# itself, and a changed page would never reach the television.
PAGES="find pipertv/static -type f | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1"
WAS_SHOWING=$(pi "cd $DIR && $PAGES" 2>/dev/null || echo "")

say "Sending $VERSION"
rsync -az --delete --exclude=__pycache__ -e "ssh -S $CONTROL -o BatchMode=yes" \
  "$STAGE/pipertv/" "$LOGIN@$HOST:$DIR/pipertv/"
ROOT_FILES=("$STAGE/main.py" "$STAGE/requirements.txt")
[ -f "$STAGE/install.sh" ] && ROOT_FILES+=("$STAGE/install.sh")
rsync -az -e "ssh -S $CONTROL -o BatchMode=yes" "${ROOT_FILES[@]}" "$LOGIN@$HOST:$DIR/"

# LC_ALL=C on both sides: the two machines collate '/' differently, which
# reorders the list and would fail this check on identical files.
SUMS="find pipertv \\( -name '*.py' -o -name '*.js' -o -name '*.css' -o -name '*.html' \\) | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1"
HERE=$(cd "$STAGE" && eval "$SUMS")
THERE=$(pi "cd $DIR && $SUMS")
[ "$HERE" = "$THERE" ] || { echo "deploy.sh: what arrived does not match what was sent." >&2; exit 1; }
echo "checksum matches: ${HERE:0:16}"

# --- stop, then start (separate connections: see bracket() above) ------------

if [ -z "$KIOSK" ]; then
  # The interface is a page a browser is already showing, so it needs
  # restarting only when that page changed. A Python change is picked up by
  # the app restart alone, and the screen never goes black.
  SHOWING=$(pi "ps -eo args | grep -c '[c]hromium --type=renderer' || true")
  if [ "${SHOWING:-0}" -lt 1 ]; then
    # Nothing is showing it -- after a reboot, or a crash. Whatever changed,
    # the interface has to be started or the television stays on the desktop.
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

# The launcher needs the desktop session's own variables to put a window on the
# TV; started over SSH it inherits none of them.
WAYLAND_ENV='export XDG_RUNTIME_DIR=/run/user/$(id -u); export WAYLAND_DISPLAY=$(ls "$XDG_RUNTIME_DIR" | grep -m1 "^wayland-[0-9]$")'

say "Starting the app"
# The channel can outlive the command when a child holds it; the app is already
# running by then, so a bounded wait is enough and the health check is the proof.
timeout 25 ssh -S "$CONTROL" -o BatchMode=yes -n "$LOGIN@$HOST" \
  "$WAYLAND_ENV; cd $DIR && setsid nohup ./.venv/bin/python3 main.py >> pipertv.log 2>&1 < /dev/null & disown; exit 0" || true
sleep 4

if [ "$KIOSK" = 1 ]; then
  say "Starting the interface on the TV"
  # The app puts it there, rather than this script spelling out a browser
  # command line of its own: whether the interface fills the screen or sits in
  # a window is a setting now, and only one of the two can be right about it.
  # It waits for the window, so this waits for it.
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
  # A restart forgets the visit, and this television never reports its selected
  # input over CEC -- it answers "switch away and back" forever -- so the gate
  # cannot open by itself and the remote is inert until someone says the Pi is
  # what the screen is showing. Deploying is that someone: it is a manual
  # confirmation, recorded as manual, not evidence pretending to be CEC.
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
