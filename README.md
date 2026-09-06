# PiperTV IR learner

Run this **Python/Flask app on your Raspberry Pi 3B+**, then open its remote control in a browser on your PC. Select an on-screen button, click **Record signal**, and press the matching button on your real remote. A TSOP2238 receiver connected to the Pi records the signal, and the app saves it on the Pi for later use.

The interface follows the supplied One For All remote photo. Some small symbols are approximate; button names can be edited. Your PC needs only a browser, and the Pi does not need a desktop environment.

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

Each capture preserves demodulated pulse/space durations in microseconds. **38 kHz is an assumed carrier frequency, not a measured one**: the TSOP2238 removes the carrier before the GPIO receives the signal. Protocol decoding and transmission are not part of this app. Replaying a recording later requires a separate IR LED transmitter and suitable driver hardware; the TSOP2238 only receives. See the [Vishay datasheet](https://www.vishay.com/docs/82459/tsop48.pdf).

## Options and checks

```bash
python3 -m pipertv --help
python3 -m unittest discover -s tests -v
```

The server listens on port 8765; use `--host` and `--port` to change its address, for example `python3 -m pipertv --host 0.0.0.0 --port 8765`. Use it on your trusted home network.

The demo exercises the interface and storage without GPIO hardware. Automated tests cover software behavior; reception still needs to be verified with your Raspberry Pi, wiring, and remote.

The local capture code and saved recordings can be reused by a future application that responds to the remote. This app provides the learning step; a future interface would add its own button recognition and actions.
