# Detecting when the TV is showing the Pi

The app offers **Pointer** or **Snapping** when HDMI-CEC reports that the TV selected the Raspberry Pi. The choice currently appears in the **open Flask recording interface**, accessible from the PC browser at `http://PI_ADDRESS:8765`. It is not an automatic native dialog over the Pi desktop.

The Python app connects this selection gate to learned IR buttons and a virtual mouse. The imported HTML prototypes remain design references. The complete live sequence of switching the TV, choosing a mode, and moving the mouse with the remote still needs to be verified on the TV in this review.

## Connected and selected are different

An HDMI cable connection or a readable display description establishes that a display is connected. It does not establish which source the TV is currently showing. The TSOP2238 receives remote button presses and cannot report the selected TV input either.

HDMI-CEC provides a separate channel for communication between HDMI devices. The detector uses CEC source/routing information to decide whether the Pi is selected. Linux exposes supported adapters through `/dev/cecX`; see the [Linux CEC introduction](https://docs.kernel.org/userspace-api/media/cec/cec-intro.html).

Detection must distinguish three outcomes:

| Outcome | Meaning | Automatic control |
| --- | --- | --- |
| Pi selected | Current source evidence identifies the Pi. | Ask for a fresh Pointer or Snapping choice before allowing control. |
| Pi not selected | Source evidence identifies another input or TV standby. | Off; discard the previous mode choice. |
| Unknown | The detector cannot establish the selected source, its evidence expired, or the connection/monitor became unavailable. | Off; a cable connection alone must not enable it. |

CEC support and reporting vary between TVs. Missing permissions, unavailable adapter support, lost monitoring, and inconclusive source messages must remain visible as unavailable or unknown states. Starting the program must not silently force the TV onto the Pi input to make detection appear successful.

**Positive CEC evidence expires after 120 seconds without a fresh relevant report.** The state then becomes Unknown and automatic control stops, even if you have not changed the input. This avoids retaining control indefinitely when a TV silently switches to its tuner or an internal app. A new report can reopen the mode choice; explicit manual confirmation is available when reporting is unreliable.

## Prepare the Pi and TV

Connect the Pi to the intended TV input and enable HDMI-CEC in the TV's settings. The setting's name depends on the TV manufacturer. On the Pi, check whether the operating system exposes an adapter:

```bash
ls -l /dev/cec*
```

An adapter node confirms that the kernel exposes CEC support; it is not a successful selected-input test. Test with the actual TV by switching between the Pi input and another source. The Pi's address in the CEC topology also must not be guessed from an example label such as “HDMI 2”.

The detector passively monitors `cec-ctl` output. If the app starts while the TV already shows the Pi, it may remain Unknown until a new source report arrives. Switch to another input and back to perform the first test. Device access and monitor permissions are covered in [section 5 of the setup guide](raspberry-pi.md#5-let-pipertv-move-the-desktop-cursor).

## Choosing the control mode

Both modes move the **actual Raspberry Pi desktop mouse** when the receiver, virtual input device, and desktop session are ready:

- **Pointer:** directional presses move the mouse freely.
- **Snapping:** directional presses move the mouse directly to another available accessibility target. Controls that the desktop or application does not expose are unavailable for snapping; see the desktop-icon limitation below.

**OK** clicks the left mouse button; **Menu** clicks the right mouse button. A new virtual pointer starts in the centre, so its first action can reposition the mouse. Recording a button temporarily suspends desktop control so the same press is not also acted on.

A newly detected switch to the Pi starts a new selection session. The previous choice must not carry over from the last visit. Losing the active Pi session clears the choice again.

The whole Raspberry Pi desktop is the first target. A later Piper TV interface can provide its own targets and actions using the same selection gate. The radial launcher, search, service launching, and pointer shown in [`../prototypes/`](../prototypes/) remain design references.

## Manual fallback

If the TV cannot reliably report its selected input, use the explicit manual confirmation in the control card, then choose the control mode. Unknown detection does not start a manual session automatically.

Manual confirmation is a user assertion, not CEC evidence. It cannot prove that the TV remains on the Pi when the TV does not report later switches. Use the visible **Stop** action before leaving a manually confirmed session.

## Check the running app

Start the app on the Pi with `python3 -m pipertv` and keep its recording interface open in the PC browser. `--demo` and `--no-control` do not start desktop control.

Open these URLs in the browser for diagnostic JSON:

- `http://PI_ADDRESS:8765/api/control`: source state and reason, the current session/mode, whether a fresh choice is needed, and CEC evidence age.
- `http://PI_ADDRESS:8765/api/health`: receiver, virtual pointer, accessibility targets, and detector health.

The mode choice is authorization for the current session; it does not prove that `/dev/uinput` is writable or that snapping targets exist. Check the runtime errors and health details if a chosen mode produces no movement.

## What this Pi actually reports

The earlier deployment notes in [`ClaudeChanges.md`](../ClaudeChanges.md) report
checks on Raspberry Pi OS (Debian 13, aarch64) running Wayland under labwc.
These observations do not replace testing the current installation; repeat the
checks if the desktop session or installed packages change.

**CEC monitoring needs a capability, not a group.** `/dev/cec0` is `root:video`
and the desktop account is already in `video`, which is enough to *open* the
device — but selecting monitor mode requires `CAP_NET_ADMIN`. An ordinary
account is refused with *"Selecting monitor mode failed, you may have to run
this as root."*, and `cec-ctl` then exits with status **0**, so only that message
reveals the failure; exit status cannot be trusted here. Granting
`cap_net_admin+ep` to the `cec-ctl` binary resolves it with no `sudo` at run
time — see [the setup guide](raspberry-pi.md). Without it the detector reports
the reason and automatic control stays off; manual confirmation remains an
explicit user action. `cec-ctl` comes from `v4l-utils`.

**The cursor moves through uinput.** This implementation uses the kernel's
`/dev/uinput` rather than a display-server pointer API. The desktop session must
accept and map the virtual device. It needs the module loaded and device access;
see [the setup guide](raspberry-pi.md). Test the actual display configuration,
especially when using scaling or multiple monitors.

**Snapping targets come from the accessibility bus, and it has limits.** The
app reads AT-SPI controls and their coordinates. The earlier deployment reported:

* An application is invisible to the bus if it was started with
  `NO_AT_BRIDGE=1`. labwc inherits that variable and passes it to everything it
  launches, which hides the whole desktop session.
* GTK tests the **value**, not the presence, so `NO_AT_BRIDGE=0` in
  `~/.config/labwc/environment` restores the bridge without removing anything.
  `toolkit-accessibility` must also be true.
* With the bridge restored, the file manager registered but exposed only its
  full-screen desktop frame — no per-icon children, even with files present on
  the Desktop. **Desktop icons are therefore not snap targets on this desktop.**
  The taskbar and ordinary GTK application windows do expose their controls with
  real coordinates, and those work.

Some inaccessible or unplaced controls report `INT32_MIN` for their position.
That sentinel and invalid/off-screen extents are discarded rather than used as
snapping coordinates. Applications can also omit controls entirely.

## Hardware acceptance checks

1. Start while the TV shows another source: control stays off.
2. Select the Pi input: confirm a source report arrives and a fresh mode choice appears in the open recording interface.
3. Choose Pointer or Snapping: only the current selection session becomes eligible for control.
4. Switch away: control turns off and the choice is cleared.
5. Switch back: the mode choice appears again rather than reusing the old value.
6. Disconnect HDMI or stop the detector: automatic control turns off.
7. Test a TV with unavailable or inconclusive CEC information: show Unknown; do not infer selection from the connection.
8. Test the explicit manual path separately from CEC detection.
9. Leave the Pi selected without fresh reports for more than 120 seconds: verify that automatic control expires to Unknown.
10. Record another remote sample, then verify that desktop control resumes correctly for the current session and never acts on the recording press itself.

Software tests can exercise state transitions and representative CEC events. Actual TV switching behavior still needs hardware verification; passing simulated events does not confirm what this TV reports.
