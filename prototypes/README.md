# Piper design references

These are the imported design files and their extracted assets. They are reference material for the future Piper TV interface; the working Python app lives in [`../pipertv/`](../pipertv/). The imported files are preserved unchanged.

## Start here

| File | What it provides |
| --- | --- |
| [Piper Final.dc.html](<tv-remote-ui/Piper Final.dc.html>) | The final **visual direction**: radial dial, enlarged central selection, QWERTY search, launch history, service catalog, and ring organization. A static arrangement of example screens. |
| [Piper Prototype.dc.html](<tv-remote-ui/Piper Prototype.dc.html>) | The main **interactive mock**: keyboard and on-screen remote navigation, simulated launches, search, ring changes, settings, and a browser-shaped pointer demo. |
| [Piper Prototype Walkthrough.html](<Piper Prototype Walkthrough.html>) | A narrated walkthrough of 13 captured prototype states. |
| [Piper Prototype.pdf](<Piper Prototype.pdf>) | The exported walkthrough for viewing or sharing without running the prototype. |
| [Piper Remote Setup.dc.html](<tv-remote-ui/Piper Remote Setup.dc.html>) | A remote setup mock, including pretend reception, button mapping, and testing. |
| [Piper TV.dc.html](<tv-remote-ui/Piper TV.dc.html>) | Earlier screen and navigation design explorations. |
| [Piper TV Prototype.dc.html](<tv-remote-ui/Piper TV Prototype.dc.html>) | An earlier interactive TV interface experiment. |
| [Raspberry Pi TV Remote UI.zip](<Raspberry Pi TV Remote UI.zip>) | Original archive, retained alongside its extracted `tv-remote-ui/` folder. |

The extracted folder also contains `support.js`, `doc-page.js`, `screens/`, and `uploads/`. Keep them alongside the HTML files. The HTML references Google Fonts; offline fonts may fall back to local alternatives.

## Preview the interactive files

From the repository root, serve the extracted folder locally:

```bash
python3 -m http.server 8767 --bind 127.0.0.1 --directory prototypes/tv-remote-ui
```

On the same computer, open [the prototype](http://localhost:8767/Piper%20Prototype.dc.html) or [the visual reference](http://localhost:8767/Piper%20Final.dc.html). This preview server only serves the imported files.

The main interactive mock uses arrow keys, Enter for OK, Backspace/Escape for Back, `h` for Home, `m` for Menu, and `0` or `c` for its pointer toggle. Its on-screen remote is clickable. In search, letter keys type into the query.

## Inspection notes

- **Ring navigation differs between sources.** The final visual reference says Right at the edge crosses to the next ring. The interactive mock's `homeKey()` wraps Left/Right within a ring and uses Down/Up to change rings; Up from the first ring opens launch history. The walkthrough and PDF describe the latter behavior. These are separate reference variants, so a future implementation must choose deliberately.
- **HDMI status is mocked.** “HDMI 2 active” is display text, and the boot progress advances on a timer. These files do not detect the TV's selected input or establish that the Pi is being watched.
- **Pairing is mocked.** The setup prototype generates sample hexadecimal codes from action names when keyboard events occur. Its `/dev/lirc0` log lines are illustrations, not actual GPIO reads. Use the Python learner for real captures.
- **Launching and service search are mocked.** Launching changes the prototype's screen state after a timer. Service names, catalog entries, and search results are example data; this does not establish installed applications, streaming support, or search API integrations on the Pi.
- **The pointer remains inside the mock.** Moving its illustrated cursor does not move the Raspberry Pi desktop mouse. Current requirements distinguish free pointer movement from snapping the actual mouse to icons, and ask for a fresh mode choice each time the TV switches to the Pi.
- **Launch history is the intended information boundary.** The reference tracks opening time and session length; it does not claim access to viewing progress inside third-party apps.

The current implementation priority is detecting whether the TV selected the Pi and gating remote control accordingly. Desktop control comes before the future Piper interface; importing these designs does not mean desktop input or the illustrated apps are implemented.
