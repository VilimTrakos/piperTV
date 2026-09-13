# PiperTV IR learner

Run this **Python/Flask app on your Raspberry Pi 3B+**, then open its remote control in a browser on your PC. Select an on-screen button, click **Record signal**, and press the matching button on your real remote. A TSOP2238 receiver connected to the Pi records the signal, and the app saves it on the Pi for later use.

The interface follows the supplied One For All remote photo. Some small symbols are approximate; button names can be edited. Your PC needs only a browser. Recording works without a Pi desktop; controlling the Pi's mouse requires a running desktop session.

## Set up the Pi

Follow the [Raspberry Pi setup and wiring guide](docs/raspberry-pi.md) to copy the project, connect the receiver, enable GPIO reception, and grant your account access to `/dev/lirc0`.

From the project directory **on the Pi**, with Python 3.10 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

If the `venv` command reports missing support, install it with `sudo apt install python3-venv` and run the commands again. Flask is the only direct Python dependency.

## Try the interface before wiring

On the Pi, with the virtual environment active:

```bash
python3 -m pipertv --demo
```

On your PC, open **`http://PI_ADDRESS:8765`**, replacing `PI_ADDRESS` with the Pi's IP address or hostname. Select a button and click **Record signal** to generate a **simulated** sample. Demo data is stored separately in `data/demo-recordings.json`; it is not a recording of your remote. Stop the app with Ctrl+C.

## Learn the real remote

After completing the GPIO setup, start the app **on the Pi**:

```bash
source .venv/bin/activate
python3 -m pipertv --device /dev/lirc0
```

`python3 main.py` starts the same app with the default receiver device.

Open `http://PI_ADDRESS:8765` in your PC browser. The Pi captures the timing locally, so network latency does not determine pulse lengths.

1. Select the corresponding on-screen remote button.
2. Click **Record signal** and wait for **Listening**.
3. Aim the real remote at the TSOP2238, briefly press the matching button, and release it.
4. Wait for **Signal saved**, then select the next button.

Multiple samples are retained per button. Record a few separate presses if you plan to build a transmitter later: protocols such as RC-5 and RC-6 can change a toggle bit between presses, and held buttons can send special repeat frames. See the [Linux IR protocol notes](https://docs.kernel.org/userspace-api/media/rc/lirc-dev-intro.html).

The default capture waits up to 10 seconds for a signal, finishes after a 120 ms quiet gap, and limits an ongoing signal to 3 seconds. Press and release once per capture to avoid combining different commands.

## Saved data and exports

Hardware recordings are saved **on the Pi** to `data/recordings.json`, relative to the directory where you start the app. Choose an explicit location for a particular remote:

```bash
python3 -m pipertv --data ~/tv-remotes/living-room.json
```

Restart with the same `--data` path to continue learning. Keep a backup of this file. The interface exports the complete JSON collection or a selected sample as an `ir-ctl` pulse/space text file; downloads are saved by the PC's browser. Renaming a button preserves its samples.

Use one running app per data file. If another app or editor changes or deletes it, PiperTV refuses to overwrite it and asks you to restart. Stop other writers, then restart with the same `--data` path to load the current contents. A hidden `.lock` file beside the JSON coordinates saves between processes; leave it in place while apps are running.

Each capture preserves demodulated pulse/space durations in microseconds. **38 kHz is an assumed carrier frequency, not a measured one**: the TSOP2238 removes the carrier before the GPIO receives the signal. Desktop control recognizes RC5 commands and compares other learned pulse timings; the saved recordings remain raw. Replaying a recording later requires a separate IR LED transmitter and suitable driver hardware; the TSOP2238 only receives. See the [Vishay datasheet](https://www.vishay.com/docs/82459/tsop48.pdf).

## Options and checks

```bash
python3 -m pipertv --help
python3 -m unittest discover -s tests -v
```

The server listens on port 8765; use `--host` and `--port` to change its address, for example `python3 -m pipertv --host 0.0.0.0 --port 8765`. Use it on your trusted home network.

The demo exercises the interface and storage without GPIO hardware. Automated tests cover software behavior; reception still needs to be verified with your Raspberry Pi, wiring, and remote.

## Control the Pi's desktop with the remote

Once buttons are learned, the same receiver can drive the Pi's own mouse pointer. Follow [section 6 of the setup guide](docs/raspberry-pi.md) to load `uinput` and install `v4l-utils`, then start the app as usual:

```bash
python3 -m pipertv
```

Control is deliberately hard to switch on by accident. It runs only while all three of these hold at once:

1. The TV reports over HDMI-CEC that it is showing the Pi's input.
2. You chose **Pointer** or **Snapping** in the browser **for that visit**.
3. No recording is in progress.

Pointer mode nudges the cursor and speeds up while a direction is held. Snapping jumps it to the next target in that direction. Switching the TV to another source turns control off and forgets the mode, so coming back asks again instead of silently resuming. The single exception is leaving a full-screen window, which works with control off so the remote is never a dead end; see the section below.

If your TV cannot report its input, the interface offers an explicit manual confirmation with a visible stop action. A manual session is your assertion, not evidence, and it is shown as such. What a TV can and cannot report is covered in [the detection notes](docs/hdmi-detection.md), including why desktop icons are not snapping targets on a Wayland session.

Start with `--no-control` to learn buttons without ever driving the desktop.

## Open a service from the interface on the TV

The Piper interface is a page served at `http://PI_ADDRESS:8765/tv`, meant to be
shown full screen on the Pi's own HDMI output. Choose **Piper interface** for the
visit and the learned remote moves the dial on the TV instead of the cursor.

**OK on YouTube opens it.** PiperTV starts chromium full screen on the Pi's
screen at YouTube's television interface, in a profile of its own so a sign-in
survives and so the window is a process PiperTV can actually close. Stopping
PiperTV closes an open service too, rather than leaving a full-screen window
nothing can dismiss.

**The way out is the one thing the gate cannot veto.** Everything on that screen
is full screen with no keyboard in front of it, so leaving has to work even when
control is off — losing the TV's report happens on its own, and it must not
trap whoever is watching:

- **Exit** or **Home** closes an open service and returns to the interface. The
  Pi acts on that press itself, because the interface is behind the service's
  window by then. **Back** is not one of them: it belongs to the service, whose
  own back button is how you leave a video.
- **Exit twice**, within six seconds and with nothing open, closes the Piper
  interface and leaves the Pi's desktop. The first press only asks; the TV shows
  the question, and any other key takes it back. One press can never do it: the
  television obeys the same remote, so exit gets pressed for other reasons.

To make this possible the receiver now listens whenever PiperTV runs, instead of
only while control is on. **Listening is not acting**: with the gate shut a
press moves no cursor, drives no interface, and is not even recorded — only the
way out is honoured. Recording a button still takes the receiver away as before.

Opening a service needs two things that the interface reports rather than
assumes: chromium installed on the Pi (`sudo apt install chromium`, or name a
different browser with `--browser`), and PiperTV running **inside the Pi's
desktop session**, since it has to put a window on that screen. Where either is
missing, the interface says so on the TV instead of appearing to open something.

Two limits are worth knowing before you try it:

- **YouTube and Prime Video are wired up.** The other tiles come from the design
  reference; Piper does not know what is installed on this Pi, so selecting one
  says it cannot open that yet. YouTube opens its ten-foot app, which it serves
  only to a browser identifying itself as a television; Prime Video has no such
  app published, so it opens the ordinary site, laid out for a pointer.
- **The remote drives what it opens**, by presenting a virtual keyboard to the
  Pi through the same kernel interface as the virtual mouse: directions, OK and
  Back arrive at YouTube's television app as arrow keys, Enter and Escape. That
  keyboard has no letters or digits — it can press only what a remote has — and
  it exists only while a service is open. Typing a search term still needs a
  keyboard, or the mouse in **Pointer** mode.
- **Launch history is kept in memory only**: restarting the app empties it.
