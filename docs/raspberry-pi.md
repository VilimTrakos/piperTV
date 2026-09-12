# Raspberry Pi 3B+ setup

The Flask app, receiver, and saved recordings all run on the Pi. Your PC opens the interface at `http://PI_ADDRESS:8765`. This guide assumes Raspberry Pi OS, Python 3.10 or newer, and a bare **Vishay TSOP2238** receiver.

## 1. Copy and install the app

Copy this project folder to the Pi, for example to `~/piperTV`, using your usual file transfer method. If you use SSH, run these commands from the project folder on your PC, replacing `USER` and `PI_ADDRESS` with your Pi login and address:

```bash
ssh USER@PI_ADDRESS 'mkdir -p ~/piperTV'
scp -r pipertv tests main.py requirements.txt README.md docs USER@PI_ADDRESS:~/piperTV/
```

On the **Pi**, open a terminal and install the Python dependencies:

```bash
cd ~/piperTV
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

If creating the virtual environment fails because `venv` is missing, install it and retry:

```bash
sudo apt update
sudo apt install python3-venv
```

You can try the interface before connecting the receiver:

```bash
python3 -m pipertv --demo
```

Open `http://PI_ADDRESS:8765` in your PC browser. `hostname -I` in a Pi terminal shows its network addresses. Demo captures are simulated and saved to `data/demo-recordings.json`. Stop the app with Ctrl+C before starting the hardware version.

## 2. Wire the TSOP2238

Shut down the Pi and disconnect power before wiring. Use these connections:

| TSOP2238 lead | Signal | Raspberry Pi 3B+ connection |
| --- | --- | --- |
| 1 | OUT | BCM GPIO17, physical header pin 11 |
| 2 | VS | 3.3 V, physical header pin 1 |
| 3 | GND | Ground, physical header pin 6 |

Identify the numbered leads using the package drawing on page 2 of the [Vishay datasheet](https://www.vishay.com/docs/82459/tsop48.pdf). **TSOP2238 has VS on lead 2 and GND on lead 3**; some similar receivers have these reversed. If yours is on a breakout board, follow that board's labelled connections.

Use **3.3 V** for this circuit. The TSOP2238 supports it, and this keeps its output compatible with the Pi's GPIO. Check header orientation against the [official Raspberry Pi GPIO documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#gpio). Keep the wires short; a 100 nF ceramic capacitor close to the receiver between VS and GND can help with supply noise.

## 3. Enable the GPIO receiver

Power the Pi on. Open its boot configuration:

```bash
sudo nano /boot/firmware/config.txt
```

Current Raspberry Pi OS uses `/boot/firmware/config.txt`; older installations may use `/boot/config.txt`. Edit the file your installation actually uses. See [Raspberry Pi's configuration documentation](https://www.raspberrypi.com/documentation/computers/config_txt.html).

Add this setting under an applicable section such as `[all]`:

```ini
[all]
dtoverlay=gpio-ir,gpio_pin=17
```

If a `gpio-ir` setting already exists, edit it instead of creating a duplicate. GPIO17 must be free for this receiver. `gpio_pin=17` is the **BCM GPIO number**, not physical pin 17. The overlay defaults to active-low reception with a pull-up, which suits the TSOP2238. See the [official overlay reference](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README).

Save the file, reboot, and check for the receiver:

```bash
sudo reboot
```

After reconnecting:

```bash
ls -l /dev/lirc*
```

The device normally appears as `/dev/lirc0`. The app reads raw timings through the kernel's LIRC interface; it does not need a `lircd` daemon or Python GPIO package. See the [Linux IR interface documentation](https://docs.kernel.org/userspace-api/media/rc/lirc-dev-intro.html).

## 4. Grant your account access

If your account can already read `/dev/lirc0`, skip this step. Otherwise, run these commands **on the Pi** to create a receiver group and persistent device permissions:

```bash
sudo groupadd --force ircapture
sudo usermod -aG ircapture "$USER"
sudo tee /etc/udev/rules.d/99-pipertv-ir.rules >/dev/null <<'RULE'
SUBSYSTEM=="lirc", KERNEL=="lirc[0-9]*", GROUP="ircapture", MODE="0660"
RULE
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=lirc
```

Here `$USER` is the current Pi account, supplied by the shell. Log out and back in so the new group membership takes effect. Check:

```bash
id
ls -l /dev/lirc0
test -r /dev/lirc0 && echo 'Receiver is readable'
```

Run the app as your normal account.

## 5. Start learning

On the **Pi**:

```bash
cd ~/piperTV
source .venv/bin/activate
python3 -m pipertv
```

`python3 main.py` is an equivalent entry point. Keep the app running, then open **`http://PI_ADDRESS:8765`** on the PC.

1. Select an on-screen remote button.
2. Click **Record signal** and wait for **Listening**.
3. Aim the real remote at the receiver and briefly press/release the matching button.
4. Wait for **Signal saved**. Select the next button and repeat.

Recordings are saved on the Pi in `~/piperTV/data/recordings.json` when started as above. Use the interface's JSON export to download them to your PC, or copy the file using your usual file transfer method. For a different location, start with `python3 -m pipertv --data ~/tv-remotes/living-room.json`.

## 6. Let PiperTV move the desktop cursor

Desktop control needs access to CEC and a virtual input device. The earlier
deployment notes describe Raspberry Pi OS (Debian 13, aarch64) with a Wayland
session under labwc; use the checks below to verify your current installation.
The mode choice appears in the open recording interface, not a native desktop
dialog. Keep that interface open on the PC while testing TV input switches.

### The virtual pointer

PiperTV moves the real cursor through `/dev/uinput`. The desktop session must
accept the virtual pointer; test movement on the display you intend to use.
If the module is not loaded and the device is root-owned, load it at boot and grant the
`input` group access:

```bash
sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/pipertv-uinput.conf
sudo tee /etc/udev/rules.d/99-pipertv-uinput.rules >/dev/null <<'RULE'
KERNEL=="uinput", SUBSYSTEM=="misc", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"
RULE
sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=uinput
```

Check the node and your group membership:

```bash
ls -l /dev/uinput                      # crw-rw---- root input
id -nG | tr ' ' '\n' | grep -x input
```

If your account is not in `input`, add it with `sudo usermod -aG input "$USER"`,
then log out and back in. The module configuration and udev rule make access
persistent. After rebooting, check that `/dev/uinput` exists and is writable by
your account.

### HDMI-CEC

`cec-ctl` comes from `v4l-utils`. The tested Pi exposed `/dev/cec0` as
`root:video`. Check the device permissions and your account's groups:

```bash
sudo apt install v4l-utils
ls -l /dev/cec0                        # crw-rw---- root video
id -nG | tr ' ' '\n' | grep -x video
```

If the device belongs to `video` and your account does not, run
`sudo usermod -aG video "$USER"`, then log out and back in.
That membership allows opening the device. Selecting monitor mode additionally
needs `CAP_NET_ADMIN`, as documented by the [Linux CEC API](https://docs.kernel.org/userspace-api/media/cec/cec-ioc-g-mode.html). As an
ordinary account `cec-ctl --monitor` prints *"Selecting monitor mode failed, you
may have to run this as root."* and exits — with status 0, so only the message
reveals it. Check yours:

```bash
timeout 5 cec-ctl -d /dev/cec0 --monitor --show-raw --skip-info
```

If that prints the refusal, automatic HDMI detection cannot work as your own
account. PiperTV reports the reason and leaves automatic control off. You can
explicitly confirm that the Pi is selected in the interface, but that manual
assertion cannot notice a silent TV input change; use **Stop** when leaving it.

Grant the capability to `cec-ctl` itself so the Flask app can stay under your
normal account. The monitor receives this capability when it runs:

```bash
sudo apt install libcap2-bin
sudo setcap cap_net_admin+ep /usr/bin/cec-ctl
sudo getcap /usr/bin/cec-ctl                 # cap_net_admin=ep
```

`setcap` and `getcap` live in `/usr/sbin`, which is normally not on a desktop
account's `PATH`; run them through `sudo` as above. Then repeat the monitor
check — it should print `Initial Event: State Change: PA: …` rather than the
refusal.

**Upgrading `v4l-utils` silently removes this.** The capability is stored on the
binary, not in a package file, so a replaced binary loses it. If HDMI detection
stops working after an update, apply `setcap` again.

Do **not** grant the same thing through a `NOPASSWD` sudoers rule instead.
PiperTV's monitor command begins with `stdbuf`, and permitting `stdbuf` as root
would effectively permit running anything as root.

### Snapping targets

Snapping reads controls through the accessibility bus, using `pyatspi` from the
[`python3-pyatspi` system package](https://packages.debian.org/trixie/python3-pyatspi).
That package depends on `python3-gi`; installing `python3-gi` alone does not install
`pyatspi`. System packages are hidden from a plain virtual environment. Install
the binding and let this environment see system packages:

```bash
sudo apt install python3-pyatspi
cd ~/piperTV
/usr/bin/python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -c "import flask, pyatspi; print('both available')"
```

Launch the app from a terminal in the **Pi's logged-in graphical session** when
testing Snapping, so it can reach that session's accessibility bus. A separate
SSH session may have different session environment variables.

Applications must register with the bus before their controls can be snapped to.
If this labwc installation disables the bridge, edit
`~/.config/labwc/environment` and set `NO_AT_BRIDGE=0`, then run
`gsettings set org.gnome.desktop.interface toolkit-accessibility true` as the
desktop user. Log out of the graphical session and back in, then reopen the
applications to apply the environment change. Preserve other entries in that
file. See [the detection notes](hdmi-detection.md) for the desktop-icon limitation.

### Check readiness and try a session

Start `python3 -m pipertv` from the project directory with the virtual environment
active. `--no-control` keeps the app in learning-only mode; `--demo` also disables
desktop control. Open `http://PI_ADDRESS:8765/api/health` to inspect pointer,
receiver, targets, and detection errors, and `/api/control` for the current
source/session state.

Keep the recording interface open on the PC. Switch away from the Pi input and
back, choose **Pointer**, and test the learned directions, **OK** for left-click,
and **Menu** for right-click. Then test Snapping against controls that appear in
the accessibility bus. Positive CEC evidence expires after 120 seconds without
a fresh relevant report; an Unknown state stops automatic control and clears
the mode choice. The full TV-switch-to-IR-movement sequence still needs live
verification on the TV.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Browser cannot reach the app | Check the Pi IP address, that the app is running, and that PC and Pi can reach each other on the network. Allow TCP port 8765 if a Pi firewall blocks it. |
| No `/dev/lirc0` | Check the active boot configuration and overlay, then reboot. Check `sudo dmesg` for `gpio-ir` or `lirc` errors. GPIO17 must be free. |
| Permission denied | Check `id`, device permissions, and the udev rule. Log in again after changing groups. |
| Capture times out | Wait for Listening before pressing. Check remote batteries, aim, lead order, 3.3 V, common ground, and GPIO17. |
| Noisy captures | Shorten wires, move away from strong light, check the supply, and press the remote once briefly. |
| Samples differ for one button | Some protocols use toggle bits or repeat frames. Keep multiple separate presses. |
| Recordings file changed | Stop other apps editing that file, then restart PiperTV to load the current version. |

The receiver is tuned to 38 kHz and returns the demodulated envelope; it cannot measure the original carrier frequency or transmit IR. Physical reception still needs to be checked with your hardware. Demo captures verify the software and browser connection.
