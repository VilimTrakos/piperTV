# PiperTV — Agent setup & deployment guide

Everything a future session needs to deploy a new version of the app to the
Raspberry Pi and understand how it runs. Written so nobody re-derives this from
scratch. **Read this before touching the Pi.**

Last verified: 2026-09-13 against the live Pi.

---

## 0. TL;DR — deploy a new version fast

The Pi is **not** a git checkout — code is pushed over SSH. Fastest safe path:

```bash
# from the project root on this machine (WSL)
# 1. push the package (rsync is simplest if installed on the Pi; else see §4)
rsync -az --delete pipertv/ rpi@192.168.1.108:~/piperTV/pipertv/
rsync -az main.py requirements.txt rpi@192.168.1.108:~/piperTV/

# 2. restart the Flask app on the Pi (NOTE the [m] trick — see §10 footgun)
ssh rpi@192.168.1.108 '
  pkill -f "[m]ain.py" ; sleep 1
  cd ~/piperTV && nohup ./.venv/bin/python3 main.py >> pipertv.log 2>&1 &
  sleep 3 && curl -s http://127.0.0.1:8765/api/health >/dev/null && echo "UP" || echo "DOWN"'

# 3. if tv.js/tv.css/tv.html changed, reload the kiosk page (see §7)
```

If `rsync` is missing on the Pi, use the base64 push in §4 (works with only
`ssh` + `base64`, and lets you verify each file with sha256).

**Password is `rpi`'s login password — never write it into a committed file.**
See §1 for the auth pattern and how to eliminate the password with a key.

---

## 1. Connecting to the Pi

| | |
|---|---|
| Host | `192.168.1.108` (WiFi, `wlan0`; `eth0` is down/no cable) |
| User | `rpi` |
| Password | the operator knows it — **do not hardcode it in any file** |
| App dir | `/home/rpi/piperTV` |
| OS | Debian 13 (trixie), aarch64, Raspberry Pi 3B+ |
| Session | Wayland under **labwc** (graphical session on the Pi's HDMI) |

**Human:** just `ssh rpi@192.168.1.108` and type the password.

**Recommended one-time fix — kill the password problem with a key:**
```bash
ssh-keygen -t ed25519 -f ~/.ssh/piper -N ''      # once, on this machine
ssh-copy-id -i ~/.ssh/piper.pub rpi@192.168.1.108 # type the password once
# then: ssh -i ~/.ssh/piper rpi@192.168.1.108      (no password afterwards)
```

**Agent (non-interactive) pattern used in this project** — no `sshpass` locally,
key auth not yet set up, so drive the password prompt over a TTY with `pexpect`,
reading the secret from an env var so it never lands in a file:

```bash
PIPER_PW=<password> python3 - <<'PY'
import os, shlex, pexpect
remote = r'''
  <your remote shell here>
'''
cmd = ("ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no "
       "-o PreferredAuthentications=password rpi@192.168.1.108 " + shlex.quote(remote))
c = pexpect.spawn(cmd, encoding="utf-8", timeout=120)
c.expect(r"[Pp]assword:"); c.sendline(os.environ["PIPER_PW"])
c.expect(pexpect.EOF, timeout=150)
print("\n".join((c.before or "").splitlines()[1:]))
PY
```

For `sudo` over that channel, use `ssh -tt` and answer the `[sudo] password`
prompt the same way. **Never pipe the password into a command that also reads
stdin** (e.g. `sudo -S ... | tee`) — that has already leaked the password into
`/etc/...` files once. Write to a temp file then `sudo cp` (cp never reads
stdin) when a privileged write is needed.

---

## 2. Architecture — what runs where

```
   PC browser  ──HTTP──>  Flask app on Pi :8765        (recording studio UI)
                              │
   Pi HDMI/TV: kiosk chromium ──> http://127.0.0.1:8765/tv?boot=0   (Piper interface)
                              │
   One For All remote ──IR──> TSOP2238 on GPIO17 ──> /dev/lirc0 ──> app
                              │
   app ──> /dev/uinput (virtual mouse)   ──> moves the real Wayland cursor
   app ──> /dev/cec0 (cec-ctl)           ──> detect which HDMI input the TV shows
   app ──> chromium kiosk (launcher.py)  ──> opens YouTube on the TV
```

- **One Flask process** serves both the studio (`/`) and the TV interface
  (`/tv`), plus JSON APIs. Port **8765**, bound `0.0.0.0`.
- The TV page **polls** `/api/tv/events?after=N`; the app never pushes.
- Everything (receiver, recordings, detection, control) lives in the app; no
  separate daemon.

---

## 3. Running the app

venv: `~/piperTV/.venv` — **Python 3.13.5**, created
`--system-site-packages` (required so `pyatspi` from the system package is
visible; plain venv hides it). Verified: `flask` and `pyatspi` both import.

```bash
cd ~/piperTV
./.venv/bin/python3 main.py            # normal run (hardware, control on)
# equivalent: ./.venv/bin/python3 -m pipertv
```

CLI flags (`pipertv/app.py :: main`):

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `0.0.0.0` | bind address |
| `--port` | `8765` | port |
| `--device` | `/dev/lirc0` | IR receiver device |
| `--demo` | off | simulate signals, no GPIO, **no desktop control** |
| `--data` | `data/recordings.json` | recordings JSON path |
| `--browser` | `chromium` | browser used to open services on the TV |
| `--no-control` | off | learn only; do not let the remote drive the desktop |

Logs: `~/piperTV/pipertv.log` (when started with the `nohup … >> pipertv.log`
form). Health check: `curl -s http://127.0.0.1:8765/api/health`,
control/detection state: `curl -s http://127.0.0.1:8765/api/control`.

**There is no autostart** — not systemd, not cron, not labwc autostart, not an
autostart `.desktop`. The app and the kiosk are started by hand. See §6 to make
it persistent, and §7 for the kiosk command.

---

## 4. Deploying a new version (detailed)

The Pi has **no git repo**, so deployment = copy files. Three options, best
first.

### Option A — rsync (preferred)
```bash
rsync -az --delete pipertv/ rpi@192.168.1.108:~/piperTV/pipertv/
rsync -az main.py requirements.txt rpi@192.168.1.108:~/piperTV/
# only if deps changed:
ssh rpi@192.168.1.108 'cd ~/piperTV && ./.venv/bin/python3 -m pip install -r requirements.txt'
```

### Option B — scp
```bash
scp -r pipertv main.py requirements.txt rpi@192.168.1.108:~/piperTV/
```

### Option C — base64 push (only ssh + base64 needed; verifies each file)
Used by this project because `sshpass` isn't installed locally and key auth
wasn't set up. Push each changed file, then compare sha256 both ends:
```bash
# per file: base64 the local file, decode it on the Pi, then sha256 both sides
LOCAL=$(sha256sum pipertv/control.py | cut -d' ' -f1)
B64=$(base64 -w0 pipertv/control.py)
ssh rpi@192.168.1.108 "echo $B64 | base64 -d > ~/piperTV/pipertv/control.py; \
  sha256sum ~/piperTV/pipertv/control.py"
# confirm the remote hash equals $LOCAL
```
(Back up first: `ssh … 'cp -a ~/piperTV/pipertv ~/piperTV/.backups/$(date -u +%Y%m%dT%H%M%SZ)/pipertv'`.)

### Restart after any push
```bash
ssh rpi@192.168.1.108 '
  pkill -f "[m]ain.py"; sleep 1
  cd ~/piperTV && nohup ./.venv/bin/python3 main.py >> pipertv.log 2>&1 &
  sleep 3; curl -s http://127.0.0.1:8765/api/health >/dev/null && echo UP || { echo DOWN; tail -20 pipertv.log; }'
```
If only static assets (`pipertv/static/tv.{js,css,html}`) changed, the Flask
restart is not enough — the kiosk is already showing the old page. Reload it
(§7).

### Run the test suite before pushing (on THIS machine)
```bash
.venv/bin/python3 -m unittest discover -s tests -t .   # 379+ tests, must be green
```
`pytest` is not installed; use `unittest`. Flask lives in the local `.venv`, not
system python.

---

## 5. Deploying a new TV-interface (static assets)

`pipertv/static/tv.html`, `tv.css`, `tv.js` are the Piper interface. Constraints
that have bitten us:

- **CSP is strict:** `style-src 'self'` — **no inline `style=` attributes** in
  markup (they silently don't apply). Set styles from JS via CSSOM
  (`el.style.x = …`) or in `tv.css`. There's a test that greps the page for
  `style=` and `<style`.
- No external CDNs; everything self-hosted.
- `?boot=0` on the URL skips the splash (the kiosk uses it so a reload doesn't
  replay the splash).

After pushing assets, reload the kiosk (§7).

---

## 6. Making it start on boot (currently NOT set up)

Cleanest is a **user** systemd service for the Flask app plus a labwc-autostart
line for the kiosk (the kiosk must run inside the graphical session). Not yet
installed; if asked to do it:

Flask app — `~/.config/systemd/user/pipertv.service`:
```ini
[Unit]
Description=PiperTV Flask app
After=network-online.target

[Service]
WorkingDirectory=/home/rpi/piperTV
ExecStart=/home/rpi/piperTV/.venv/bin/python3 main.py
Restart=on-failure

[Install]
WantedBy=default.target
```
```bash
systemctl --user enable --now pipertv.service
loginctl enable-linger rpi        # so it runs without an active login
```

Kiosk — append to `~/.config/labwc/autostart` (create if absent). Use the
command in §7. labwc autostart runs inside the Wayland session, so
`WAYLAND_DISPLAY` is already set there.

---

## 7. The kiosk browser (Wayland)

The TV interface is a **real chromium window in the Pi's labwc Wayland session**,
full-screen kiosk, pointed at the local app. The **known-good** invocation
(captured live from the running process) — essential flags:

```bash
chromium \
  --ozone-platform=wayland \        # Wayland, not X11
  --kiosk --start-fullscreen \
  --disable-gpu \                   # REQUIRED: Pi 3B+ GPU EGL init fails otherwise
  --password-store=basic \          # REQUIRED: avoids a GNOME-keyring prompt that hangs
  --no-first-run --noerrdialogs --no-default-browser-check \
  --force-renderer-accessibility \  # helps AT-SPI see the page
  --user-data-dir=/tmp/kiosk-gpu-off \
  'http://127.0.0.1:8765/tv?boot=0'
```

Why the two REQUIRED flags:
- **`--disable-gpu`** — without it chromium logs
  `eglCreateContext … EGL_BAD_ATTRIBUTE` / `Exiting GPU process due to errors`
  and the page never renders on the Pi 3B+.
- **`--password-store=basic`** — without it chromium blocks on the desktop
  keyring unlock dialog (no keyboard on the TV to answer it).

**Launching the kiosk from an SSH session** (not from the graphical session)
needs the Wayland env of that session, or chromium can't find the compositor:
```bash
ssh rpi@192.168.1.108
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export WAYLAND_DISPLAY=$(ls "$XDG_RUNTIME_DIR" | grep -m1 '^wayland-[0-9]')
chromium --ozone-platform=wayland --kiosk --disable-gpu --password-store=basic \
  --user-data-dir=/tmp/kiosk-gpu-off 'http://127.0.0.1:8765/tv?boot=0' &
```
`launcher.py` (the app opening YouTube) has the same requirement: it refuses if
neither `WAYLAND_DISPLAY` nor `DISPLAY` is set, telling you to start Piper from
the desktop session.

Reload the kiosk after an asset change (simplest = restart it):
```bash
pkill -f "[k]iosk-gpu-off"      # kill the kiosk chromium (matches its user-data-dir)
# then relaunch with the command above (from the graphical session or via the SSH env trick)
```

Screenshots of the Wayland session (for verification): `grim ~/shot.png`
(`grim` is the Wayland framebuffer grabber; scrot/import won't work under
Wayland).

---

## 8. One-time system setup (already done on this Pi — re-check, don't redo blindly)

Full detail in the recovered `docs/raspberry-pi.md` (in git HEAD). Condensed:

**venv (system site packages for pyatspi):**
```bash
sudo apt install python3-pyatspi
cd ~/piperTV && /usr/bin/python3 -m venv --system-site-packages .venv
./.venv/bin/python3 -m pip install -r requirements.txt
```

**IR receiver (TSOP2238 on BCM GPIO17):** in `/boot/firmware/config.txt`:
`dtoverlay=gpio-ir,gpio_pin=17`; device is `/dev/lirc0`, group `ircapture`
(udev rule `/etc/udev/rules.d/99-pipertv-ir.rules`, `MODE=0660`). No lircd
needed — the app reads raw LIRC MODE2.

**Virtual pointer (`/dev/uinput`):** works identically on X11 and Wayland (raw
ioctls in `pointer.py`). Persistent access:
```
/etc/modules-load.d/pipertv-uinput.conf   ->  uinput
/etc/udev/rules.d/99-pipertv-uinput.rules ->  KERNEL=="uinput", SUBSYSTEM=="misc", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"
```
Account must be in group `input`.

**HDMI-CEC (`/dev/cec0`, from `v4l-utils`):** monitor mode needs
`CAP_NET_ADMIN` (group `video` only grants opening the device). Granted on the
binary:
```bash
sudo setcap cap_net_admin+ep /usr/bin/cec-ctl
sudo getcap /usr/bin/cec-ctl        # cap_net_admin=ep
```
**Upgrading `v4l-utils` silently drops this** (capability is on the binary) —
re-apply after any update. `cec-ctl` exits 0 even when it refuses monitor mode,
so only the printed message reveals a failure. Do **not** use a NOPASSWD sudoers
rule instead (the monitor command starts with `stdbuf`, which would be an
arbitrary-root hole).

**AT-SPI (Snapping targets):** `~/.config/labwc/environment` sets
`NO_AT_BRIDGE=0` (GTK checks the *value*, not presence). Also
`gsettings set org.gnome.desktop.interface toolkit-accessibility true`.
Known limitation: **desktop icons are not snappable** — pcmanfm exposes only its
frame (`kids=0`), even with real Desktop files present.

---

## 9. Wayland vs X11 — what actually matters here

- **Virtual mouse:** `/dev/uinput` is a kernel interface — the same code moves
  the cursor on **both** X11 and Wayland. An absolute axis maps onto the whole
  screen on both. No X11-specific path.
- **This Pi runs Wayland/labwc**, so: chromium needs `--ozone-platform=wayland`;
  screenshots use `grim`; `WAYLAND_DISPLAY` (not `DISPLAY`) identifies the
  session; GUI things launched over SSH need `XDG_RUNTIME_DIR` +
  `WAYLAND_DISPLAY` exported (§7).
- **AT-SPI** works the same on both; the bridge just has to be enabled.
- If this were ever moved to an X11 session: set `DISPLAY`, drop
  `--ozone-platform=wayland`, use `scrot`/`import` instead of `grim`. Nothing
  else changes — uinput and AT-SPI are unaffected.

---

## 10. Footguns (learned the hard way)

- **`pkill -f main.py` kills your own SSH command** — the remote shell text
  contains "main.py", so pkill matches it and kills the shell mid-run (the app
  restart line never runs → app ends up DOWN). Always use a bracket:
  `pkill -f "[m]ain.py"`. Same for the kiosk: `pkill -f "[k]iosk-gpu-off"`.
- **The agent's own Bash is blocked by the auto classifier for
  network-interception patterns** (nftables NAT, `tcpdump`, `arpspoof`,
  forwarding) — even read-only `nft list` / `tcpdump` were denied. Those steps
  must be **run by the user directly on the Pi**; the agent can only guide.
- **Never write the SSH password into a file** (already leaked into
  `/etc/udev/rules.d/...` once via `sudo -S | tee`). Use temp-file + `sudo cp`
  for privileged writes.
- **`setcap` is lost on `v4l-utils` upgrade** (see §8).
- **CSP blocks inline `style=`** in the TV page (§5).
- **`--system-site-packages` is mandatory** or `pyatspi` vanishes from the venv.
- Recordings file is guarded by a sha256 check — external edits make the app
  refuse to save and ask for a restart; don't hand-edit
  `data/recordings.json` while the app runs.

---

## 11. Reference — helper scripts already on the Pi (`~/piperTV/`)

| File | What it does |
|---|---|
| `open-control.sh [pointer\|snapping\|piper]` | Opens desktop control without the PC browser: manual-confirms the session and picks a mode. Use after a restart/TV power-cycle (which closes the gate). |
| `cec-test.sh` | Registers the Pi as a CEC playback device + follower and monitors, to test whether the TV forwards its remote (it does **not** — see §12). |
| `tv-ws.py [v+\|v-\|mute\|listen N]` | Talks to the TV's WebSocket remote (see §12). |
| `tv-keys.py [keys…]` | Sends a sequence of `{"event":…}` keys to the TV over WebSocket. |
| `captures/` | pcap output dir (from the firmware-capture experiments). |

---

## 12. Reference — findings a future session should NOT re-derive

**The remote conflict (the core problem).** The One For All is programmed to the
Grundig's own code: **every one of the 31 learned buttons decodes as RC5
address 0** — the TV's own address. So the TV obeys the remote directly
(e.g. left/right = volume) no matter what Piper does. Nothing on the Pi can stop
that. **Solution shipped:** a *role* layer (`pipertv/roles.py`) — bind a Piper
role (`up`, `ok`, …) to a button the TV ignores (the playback block: play,
pause, previous, next, 3D, rewind, fast_forward). Rebinding a role also silences
its original key. Managed via `GET/PUT /api/roles/<role>`; suggested map is the
playback cross (`roles.SUGGESTED`). **Still TODO:** the 7 playback carrier
buttons aren't recorded yet — record them in the studio, then bind. Until then
bindings are empty (reverted, so the remote still works as before).

**HDMI-CEC is a dead end for control.** The Grundig answers a couple of CEC
queries (vendor id `0x00d0d5`, power status) but times out / Feature-Aborts on
most, has **no RC passthrough**, and doesn't forward its remote keys as
`<User Control Pressed>`. CEC can't report the active input either. Don't spend
time here again.

**The TV IS network-controllable (Vestel/Grundig).** At `192.168.1.134`
(MAC `b8:b7:f1:4d:0e:5a`), open ports 443/8080/56789/56790.
- `wss://192.168.1.134/foo` (TLS, self-signed) — send `{"event":"v+"}` /
  `v-` / `mute` / `memc` / `exit`. **Volume/mute confirmed working.** It's
  **write-only**: replies `server ready` on connect, then nothing — no state is
  ever reported, no navigation events exist, and it never announces the remote.
- `https://192.168.1.134/list` — returns 115 channels as JSON (`channellist`),
  but **no "current input / channel" marker anywhere**, so it can't fix
  detection.
- No SSH/telnet/ADB on the TV (full port scan 1–1024 + common high ports).
- Idea if wanted: a `television.py` that sends volume/mute over this WebSocket
  so those keys leave the IR conflict entirely. Not built yet.

**Branch/tests.** Work is on branch `desktop-control`. Local suite is
`unittest` (379+ tests green). The role feature added `roles.py`,
`test_roles.py`, `test_storage_roles.py`, `test_control_roles.py`,
`test_app_roles.py`; touched `storage.py`, `tv.py`, `ir_control.py`,
`control.py`, `app.py`. `docs/raspberry-pi.md` and `docs/hdmi-detection.md` were
removed from the working tree but remain in git HEAD (recover with
`git show HEAD:docs/raspberry-pi.md`).
