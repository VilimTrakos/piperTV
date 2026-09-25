# What this television can be told, and what it cannot

Measured on a **Grundig 32 VLE 6735 BP**, software `G7GRMR.N.-.-.-.V11.000.00`,
bootloader `BOOT.G7.N.-.-.-.V11.000.00`, board `SINGLETUNER BOARD`, compiled
18 April 2019. Grundig is Arçelik, not Vestel: guides written for Vestel sets
(profile files, `MENU + 4725`, USB Operations) do not apply here, which cost an
afternoon to establish.

Everything below was found by asking the set, on the same network. None of it
needs modified firmware.

**This is a record of measurements, not a list of features.** Piper implements
none of it. What was built on top of these findings -- opening a service on the
Pi for a dial shown in the set's own browser, and asking the set to switch to
this input -- was taken out again, because the only television available to try
it on is the one whose browser cannot show the interface in the first place.
The findings are kept because they are hard to come by and because they explain
why Piper does not attempt HDMI-CEC here.

## The short version

| Want | How | Works here |
|---|---|---|
| Open Netflix or YouTube on the TV | DIAL, over the network | **yes** |
| Close what the TV opened | DIAL `DELETE`, or the `exit` event | **yes** |
| Mute, volume, backlight, power | the set's own WebSocket | **yes** |
| The channel list | `https://<tv>/list` | **yes** |
| Wake it from standby | Wake-on-LAN, MAC from DIAL | untested |
| Make it switch to the Pi's input | **HDMI signal off and on** | **yes** |
| Make it switch with HDMI-CEC | `ACTIVE_SOURCE` and friends | **no** |
| Know which input it is showing | nothing reports it | **no** |
| Add Prime Video, HBO Max, any app | nothing | **no** |

## DIAL: the television's own applications

The set answers an SSDP search for `urn:dial-multiscreen-org:service:dial:1`
with a description at `http://<tv>:56790/dd.xml`, whose `Application-URL`
header points at `http://<tv>:56789/apps/`.

```
GET    /apps/YouTube        <state>stopped</state> | running, and allowStop="true"
POST   /apps/YouTube        201 Created -- the set opens its own app
DELETE /apps/YouTube/run    closes it
```

Only **YouTube** and **Netflix** are there. Prime Video, HBO Max, Disney+ and
the rest answer 404: DIAL starts what the firmware already has and installs
nothing. The description also carries `wolMac`, so the set can be woken from
standby with a magic packet.

This is why those two tiles open on the television rather than on the Pi. The
set decodes in hardware and holds its own licences, which a Pi 3B+ rendering
the same service as a web page does not.

## The remote tester: a WebSocket the set left open

The set serves a developer page on `https://<tv>/` (port 443, a page dated
2019) which drives it over `wss://<tv>/foo` with JSON events:

```json
{"event":"power"}  {"event":"mute"}  {"event":"v+"}  {"event":"v-"}
{"event":"bl+"}    {"event":"bl-"}   {"event":"memc"} {"event":"exit"}
```

`mute` and `exit` are confirmed by observation -- `exit` closed the running
YouTube app. Unknown event names are ignored in silence, so the vocabulary
cannot be enumerated by asking; `source`, `input`, `src` and `hdmi1` were
tried and did nothing. The socket announces `server ready` on connect and
says nothing else, including when the input is changed by hand.

## Switching the input: the HDMI cable, not CEC

The service menu explains the CEC dead end in one line:

> **CEC Library Version: `-----`**

The firmware has no CEC stack. The set's chip acknowledges messages on the bus,
so `cec-ctl` reports a successful transmit, and nothing acts on them:
`IMAGE_VIEW_ON`, `ACTIVE_SOURCE`, `ROUTING_CHANGE`, `SET_STREAM_PATH` and
`TEXT_VIEW_ON` were all sent and all ignored. Nothing is reported back either,
which is why HDMI detection never had anything to go on.

What the set does obey is a signal appearing:

```bash
wlr-randr --output HDMI-A-1 --off ; sleep 4 ; wlr-randr --output HDMI-A-1 --on
```

It switches to this input by itself when the picture comes back -- which is the
mechanism anyone building this arrangement would use, in place of the CEC
messages the set ignores. There is no equivalent for leaving: coming back to
the set's own apps is its Source button.

## The service menu

`MENU` then `8500` on the set's own remote, in quick succession. The Vestel
codes (`4725`, `2483`) do nothing on this set. Inside, most entries want a
second code:

| Entry | Code |
|---|---|
| System Configuration | none |
| Software Version | none |
| Source, TV, Sound, Video, Panel, Acoustic, EMC | `2356` |
| Cloner Configuration | `4658` |
| Approved Logos | `2354` |
| Boot Code, Debug, Network, StageCraft | unknown |

**`Plug Play` is not a setting but a trigger.** Turning it on in System
Configuration immediately returns the set to its out-of-box state: the remote
stops responding, the network configuration is gone, and the first-install
wizard has to be completed again -- language, country, network, channel scan.
Nothing is damaged and the applications survive, but an evening's channel list
does not.

There is no profile export here, and no feature flags for applications:
`Cloner Configuration` copies settings and channels to a USB stick, which makes
it worth using as a backup and useless as a way to find a hidden Prime Video.

## Two things that bit, and what they were

**The television's own browser cannot show Piper's interface.** It is QtWebKit
4.8 from 2012: no CSS custom properties, no `calc()`, no flexbox, and no
JavaScript newer than ES5. It reports a 1024x448 window and grows when its
toolbars are hidden. It cannot show a modern streaming site either, which is
what sends those tiles to the Pi. See `pipertv/static/classic.js`, which is
written for it, and the test that keeps it that way.

**After a factory setup the set stopped sending EDID.** The Pi then falls back
to a generic VESA list whose best mode is 1024x768. Forcing the mode restores
the size but not the timings, so the picture is soft:

```bash
wlr-randr --output HDMI-A-1 --custom-mode 1920x1080@50
```

The real fix is at the set: reseat the cable, and check the per-input HDMI mode
and picture format. Labelling the input as a PC also turns off the sharpening
that makes text look soft at the correct resolution.
