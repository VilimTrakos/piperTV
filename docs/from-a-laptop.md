# Use Piper from a laptop

Piper normally puts its own interface on the television: a browser window on
the Pi's HDMI output, a cursor it moves, services it starts there. Served mode
is the other arrangement. The Pi keeps the two things only it can do — the
receiver and the recordings — and the interface is opened from a laptop, a
tablet, or anything else on the same network. The remote still drives it, and
the services open in tabs of the browser that is showing the page.

Two reasons to want it. A Pi 3B+ has less than a gigabyte of memory and the
interface alone is the largest thing in it, so a Pi that shows nothing has all
of it for whatever else you ask of it — and the learned remote is usable from
the sofa, the desk, or anywhere the Pi's screen is not.

## Start it

On the Pi:

```bash
source .venv/bin/activate
python3 -m pipertv --served
```

Then open **`http://PI_ADDRESS:8765/tv`** in a browser on the same network. The
studio at `http://PI_ADDRESS:8765` is unchanged: recording buttons, bindings
and settings all work as they always did.

From a PC with the project checked out, `./deploy.sh --served` does the same
over SSH — it stops whatever the Pi was running, including the interface on the
television, and starts it served instead.

Nothing here needs the Pi's desktop session, so this also runs on a Pi with no
screen attached at all, started over SSH.

## What the remote does

Everything the dial does on the television, it does in the browser showing the
page: the ring turns, OK opens, left twice opens the options, and holding a
direction repeats. The presses travel as data — the Pi reports which button was
pressed and what it performs, and the page acts on it.

A press only counts while that tab is the one you are looking at. Switch to
another tab and the remote stops driving Piper, which is what makes it safe to
leave the page open.

**A tile opens a tab.** The Pi opens nothing. Exit or home closes the tab Piper
opened and leaves you back on the dial, which is the only way to close a tab
with a remote in your hand.

**Allow pop-ups for the Pi's address.** A tab opened by a press of the remote is
not a click, and browsers block tabs nobody asked for by hand. When that
happens the notice offers the service as a link: clicking it opens the tab, and
allowing pop-ups for this address once settles it for good.

## What is not here

These belong to the Pi's own screen, and served mode says so rather than
pretending:

- **The cursor.** Pointer and snapping modes move the mouse on the Pi's
  desktop. On a laptop you already have a mouse, and the page is an ordinary
  page: click it.
- **Kodi.** It plays video in the Pi's own hardware, which is the whole reason
  it has a tile. A browser somewhere else has nothing to show of it, so the
  tile says so.
- **The on-screen keyboard**, and **leaving Piper for the desktop**. Both are
  about a television with no keyboard in front of it. Here, type.
- **The control gate.** It exists so a press cannot move this Pi's cursor while
  the television is showing something else. A press in served mode reaches a
  web page and nothing else, so there is no HDMI report to wait for and no mode
  to choose: open the page and the remote drives it.

The studio's control panel reports that this server has no desktop control,
which is exactly what it has.

## Back to the television

Start it again without `--served` (or deploy without it). The two are simply
two ways to run the same program, and the recordings, bindings and settings are
the same file either way.
