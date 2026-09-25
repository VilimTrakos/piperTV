#!/usr/bin/env bash
# Install PiperTV on a Raspberry Pi, in one go.
#
# In a terminal on the Pi's own desktop:
#
#   curl -fsSL https://raw.githubusercontent.com/VilimTrakos/piperTV/main/install.sh | bash
#
# or from a copy of the project:
#
#   ./install.sh               install; the one question is which pin the IR receiver is on
#   ./install.sh --pin 27      the receiver's OUT wire is on GPIO27 (default: auto)
#   ./install.sh --yes         ask nothing, take every default
#   ./install.sh --dry-run     say what would be done, and change nothing
#
# Run it as the desktop user, not as root; it uses sudo where needed. Running
# it again only does what is still missing.
set -euo pipefail

USER=${USER:-$(id -un)}
REPO=${PIPER_REPO:-https://github.com/VilimTrakos/piperTV}
TARGET=${PIPER_DIR:-$HOME/piperTV}
PIN=""; YES=0; DRY=0
ARGS=("$@")

while [ $# -gt 0 ]; do
  case "$1" in
    --pin) PIN=${2:?--pin needs a GPIO number, or auto}; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "install.sh: unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
done

if [ -t 1 ]; then
  BOLD=$'\e[1m'; DIM=$'\e[2m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; OFF=$'\e[0m'
else
  BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; OFF=""
fi
step() { printf '\n%s== %s%s\n' "$BOLD" "$*" "$OFF"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
note() { printf '  %s!%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()  { printf '\n%sinstall.sh: %s%s\n' "$RED" "$*" "$OFF" >&2; exit 1; }
# Report something done (in a dry run nothing was).
did()  { [ "$DRY" = 1 ] || ok "$@"; }

# Everything that changes the system goes through act, so --dry-run can print it instead.
act() {
  if [ "$DRY" = 1 ]; then printf '  %swould run:%s %s\n' "$DIM" "$OFF" "$*"; else "$@"; fi
}
root() { act sudo "$@"; }

# Write a root-owned file. The text goes through a temporary file, never through
# sudo's stdin, where a password typed at the wrong moment would end up in it.
CHANGED=0
root_file() {
  local path=$1 mode=$2 text=$3 temporary
  if [ -f "$path" ] && [ "$(cat "$path")" = "$text" ]; then
    ok "$path is in place"
    return 0
  fi
  CHANGED=1
  if [ "$DRY" = 1 ]; then
    printf '  %swould write:%s %s\n' "$DIM" "$OFF" "$path"
    return 0
  fi
  temporary=$(mktemp)
  printf '%s\n' "$text" > "$temporary"
  sudo install -m "$mode" "$temporary" "$path"
  rm -f "$temporary"
  ok "wrote $path"
}

# Ask on the terminal (also under curl | bash). With --yes or without a
# terminal, the default is the answer.
ask() {
  local answer=""
  if [ "$YES" = 0 ] && { exec 3</dev/tty; } 2>/dev/null; then
    printf '  %s ' "$1" > /dev/tty
    read -r answer <&3 || answer=""
    exec 3<&-
  fi
  printf '%s' "${answer:-$2}"
}

installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
in_group() { id -nG "$USER" | tr ' ' '\n' | grep -qx "$1"; }

# --- first, what this is running on ----------------------------------------

[ "$(id -u)" -ne 0 ] || die "run this as the desktop's own user, not as root; it asks for sudo itself."
command -v apt-get >/dev/null || die "this installs with apt, as on Raspberry Pi OS, and apt-get is not here."
MODEL=$({ tr -d '\0' < /proc/device-tree/model; } 2>/dev/null || true)
case "$MODEL" in
  *"Raspberry Pi"*) ;;
  *) note "this does not look like a Raspberry Pi (${MODEL:-unknown machine}); carrying on" ;;
esac
[ "$DRY" = 0 ] || note "a dry run: nothing below is changed, only said"

# --- the program itself ------------------------------------------------------

# Run from a checkout, install that; run through a pipe, clone the project first.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)
if [ -n "$HERE" ] && [ -f "$HERE/pipertv/app.py" ]; then
  DIR=$HERE
else
  step "Getting PiperTV"
  if ! command -v git >/dev/null; then
    root apt-get install -y git
  fi
  if [ -d "$TARGET/.git" ]; then
    act git -C "$TARGET" pull --ff-only
  elif [ -f "$TARGET/pipertv/app.py" ]; then
    note "$TARGET is a copy without git history; installing it as it is"
  elif [ -e "$TARGET" ]; then
    die "$TARGET exists and is not PiperTV; move it aside, or set PIPER_DIR."
  else
    act git clone "$REPO" "$TARGET"
  fi
  # Carry on with the installer from the fetched copy.
  if [ -f "$TARGET/install.sh" ]; then
    exec bash "$TARGET/install.sh" "${ARGS[@]}"
  fi
  if [ ! -f "$TARGET/pipertv/app.py" ]; then
    note "a dry run stops here: nothing was fetched to look at"
    exit 0
  fi
  DIR=$TARGET
fi
cd "$DIR"
printf '%sPiperTV%s in %s\n' "$BOLD" "$OFF" "$DIR"

# --- 1. what Piper runs on ---------------------------------------------------

step "Programs"
# Python with GTK and AT-SPI, the on-screen keyboard (wvkbd), wlr-randr for the
# screen size, cec-ctl (v4l-utils), setcap, and chromium (it has two package names).
NEEDED=(python3-venv python3-gi gir1.2-gtk-3.0 python3-pyatspi wvkbd wlr-randr
        v4l-utils libcap2-bin)
if installed chromium-browser; then NEEDED+=(chromium-browser); else NEEDED+=(chromium); fi
# Optional; the tile says so when one is missing: Firefox, Kodi, and Widevine
# for Netflix, Prime Video, Disney+ and HBO Max.
EXTRAS=(firefox kodi libwidevinecdm0)
missing=(); extras=()
for package in "${NEEDED[@]}"; do installed "$package" || missing+=("$package"); done
for package in "${EXTRAS[@]}"; do installed "$package" || extras+=("$package"); done
if [ ${#missing[@]} -eq 0 ] && [ ${#extras[@]} -eq 0 ]; then
  ok "everything Piper uses is installed"
else
  root apt-get update
  if [ ${#missing[@]} -gt 0 ]; then
    root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
  fi
  for package in "${extras[@]}"; do
    root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$package" \
      || note "$package could not be installed; its tile will say so"
  done
  did "installed ${missing[*]} ${extras[*]}"
fi

# --- 2. what this user may use -----------------------------------------------

step "Permissions"
RELOGIN=0
# gpio: the IR receiver's pin. input: the virtual mouse and keyboard.
# video: HDMI-CEC, for knowing when the TV shows the Pi.
for group in gpio input video; do
  if ! getent group "$group" >/dev/null; then
    note "there is no $group group on this system; skipping it"
  elif in_group "$group"; then
    ok "$USER is in $group"
  else
    root usermod -aG "$group" "$USER"
    RELOGIN=1
    did "added $USER to $group"
  fi
done

CHANGED=0
root_file /etc/modules-load.d/pipertv-uinput.conf 0644 "uinput"
root_file /etc/udev/rules.d/99-pipertv-uinput.rules 0644 \
  'KERNEL=="uinput", SUBSYSTEM=="misc", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"'
if [ "$CHANGED" = 1 ]; then
  root modprobe uinput
  root udevadm control --reload-rules
  root udevadm trigger --name-match=uinput
fi

# CEC monitor mode needs CAP_NET_ADMIN on cec-ctl. Upgrading v4l-utils drops
# it; running this script again puts it back.
CEC=$(command -v cec-ctl || true)
GETCAP=$(command -v getcap || echo /usr/sbin/getcap)
if [ -z "$CEC" ]; then
  note "cec-ctl is missing; Piper will not know which input the TV shows"
elif "$GETCAP" "$CEC" 2>/dev/null | grep -q cap_net_admin; then
  ok "cec-ctl may watch the TV's input"
else
  root setcap cap_net_admin+ep "$CEC"
  did "cec-ctl may now watch the TV's input"
fi

# Only if config.txt sets up the kernel's IR receiver. Otherwise Piper reads
# the pin itself, which only needs the gpio group.
BOOT=/boot/firmware/config.txt
[ -f "$BOOT" ] || BOOT=/boot/config.txt
if grep -qs '^[[:space:]]*dtoverlay=gpio-ir' "$BOOT"; then
  if ! getent group ircapture >/dev/null; then root groupadd --force ircapture; fi
  if in_group ircapture; then ok "$USER is in ircapture"; else
    root usermod -aG ircapture "$USER"; RELOGIN=1; did "added $USER to ircapture"
  fi
  CHANGED=0
  root_file /etc/udev/rules.d/99-pipertv-ir.rules 0644 \
    'SUBSYSTEM=="lirc", KERNEL=="lirc[0-9]*", GROUP="ircapture", MODE="0660"'
  root_file /etc/udev/rules.d/99-pipertv-ir-protocols.rules 0644 \
'# Raw LIRC only. Otherwise the kernel also decodes the remote into key presses
# and the desktop gets every press twice.
ACTION=="add", SUBSYSTEM=="rc", KERNELS=="ir-receiver@*", ATTR{protocols}="lirc"'
  if [ "$CHANGED" = 1 ]; then
    root udevadm control --reload-rules
    root udevadm trigger --subsystem-match=lirc --subsystem-match=rc
  fi
else
  ok "no kernel IR receiver in $BOOT; Piper reads the receiver's pin itself"
fi

# --- 3. Piper's own Python ---------------------------------------------------

step "Python"
# --system-site-packages: GTK and pyatspi come from apt, not from PyPI.
if [ -x .venv/bin/python3 ] && .venv/bin/python3 -c "import gi, flask" 2>/dev/null; then
  ok "the Python environment is ready"
else
  act python3 -m venv --system-site-packages .venv
  act .venv/bin/python3 -m pip install --quiet --disable-pip-version-check -r requirements.txt
  did "the Python environment is ready"
fi

# --- 4. the one question -----------------------------------------------------

step "IR receiver"
if [ -z "$PIN" ] && [ -f pipertv.conf ]; then
  ok "kept as pipertv.conf has it: $(grep -m1 '^[[:space:]]*pin' pipertv.conf || echo 'pin = auto')"
else
  if [ -z "$PIN" ]; then
    PIN=$(ask "Which GPIO is the receiver's OUT wire on? Enter for auto (GPIO17, physical pin 11):" auto)
  fi
  if [ "$DRY" = 1 ]; then
    printf '  %swould write:%s pin = %s in %s/pipertv.conf\n' "$DIM" "$OFF" "$PIN" "$DIR"
  else
    .venv/bin/python3 - "$PIN" <<'PY' || die "that is not a pin Piper can read; try auto, or a number from 2 to 27."
import sys
from pipertv.config import CONFIG, write_pin
try:
    print(f"  ✓ pin = {write_pin(sys.argv[1])} in {CONFIG}")
except ValueError as exc:
    sys.exit(f"  {exc}")
PY
  fi
fi

# --- 5. the desktop ----------------------------------------------------------

step "Desktop"
# Icons, the login entry and the desktop settings, all in the user's home (see pipertv/start.py).
if [ "$DRY" = 1 ]; then
  act .venv/bin/python3 -m pipertv.start --install
else
  .venv/bin/python3 -m pipertv.start --install \
    | sed -e "s/^Wrote /  $GREEN✓$OFF wrote /" -e t -e "s/^/  /"
  # The window rules take effect at once; the rest at the next login.
  pkill -HUP -x labwc 2>/dev/null || true
fi

# --- done --------------------------------------------------------------------

ADDRESS=$(hostname -I 2>/dev/null | awk '{print $1}')
step "Done"
cat <<EOF
  From now on the remote moves the mouse from the moment the desktop starts,
  and the PiperTV icon -- on the panel, the desktop or in the menu -- starts Piper.

  The first time, teach Piper your remote: in Piper, options > the remote
  (a mouse helps for this one visit), or from a computer at http://${ADDRESS:-this-pi}:8765
EOF
if [ "$DRY" = 1 ]; then
  exit 0
fi
if [ "$RELOGIN" = 1 ]; then
  printf '\n  %sThe Pi has to restart once, so the new permissions apply.%s\n' "$BOLD" "$OFF"
  case "$(ask "Restart now? [Y/n]" y)" in
    [nN]*) echo "  Restart it when you are ready: sudo reboot" ;;
    *) sudo reboot ;;
  esac
elif [ -n "${WAYLAND_DISPLAY:-}" ]; then
  case "$(ask "Start Piper now? [Y/n]" y)" in
    [nN]*) ;;
    *) .venv/bin/python3 -m pipertv.start || note "Piper did not start; the end of pipertv.log says why" ;;
  esac
fi
