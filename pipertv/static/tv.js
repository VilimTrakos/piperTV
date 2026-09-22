"use strict";

/* The Piper interface, driven by the learned remote.
 *
 * Presses arrive as numbered data from /api/tv/events rather than as
 * synthesised keystrokes, so this page can be developed and judged with a
 * keyboard and behaves the same either way.
 *
 * The service list below is still the one from the design references: Piper
 * does not yet know what is installed on this Pi. Which of them it can actually
 * open comes from the server, and OK asks the server to open it, so a tile that
 * leads nowhere says so rather than pretending. The launch history underneath
 * is whatever Piper really started, not an illustration.
 */

(() => {
  const $ = (id) => document.getElementById(id);

  const SERVICES = [
    { id: "search", name: "Search", letter: "⌕", colour: "#1B1F27", kind: "search" },
    { id: "netflix", name: "Netflix", letter: "N", colour: "#A8382F" },
    { id: "youtube", name: "YouTube", letter: "Y", colour: "#C4552F" },
    { id: "prime", name: "Prime Video", letter: "P", colour: "#1E8496" },
    { id: "disney", name: "Disney+", letter: "D", colour: "#2F4A9C" },
    { id: "hbo", name: "HBO Max", letter: "H", colour: "#6B3FA0" },
    { id: "voyo", name: "Voyo", letter: "V", colour: "#A8385A" },
    { id: "kodi", name: "Kodi", letter: "K", colour: "#3B7A57" },
    { id: "browser", name: "Web browser", letter: "L", colour: "#5A6570" },
  ];

  const BY_ID = new Map(SERVICES.map((service) => [service.id, service]));

  // Design coordinates; the stylesheet scales them through --u.
  const CENTRE = { x: 960, y: 560 };
  const RADIUS = 300;
  const TILE = 120;
  const FOCUSED_TILE = 200;

  const KEYS = {
    ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
    Enter: "ok", Backspace: "back", Escape: "back", h: "home", m: "menu",
  };

  const state = { screen: "boot", focus: 1, seen: 0, stream: null,
    primed: false, control: "off", tiles: [], session: null, services: null,
    open: null, opening: false, notice: "", serviceError: null };
  let noticeTimer = null;

  // Two presses of left, close enough together to be one gesture, open the
  // options. The first of them has already moved the wheel, so the gesture
  // puts it back: nobody asked to change what is selected.
  const DOUBLE_LEFT_MS = 450;
  let lastLeftAt = 0;
  let focusBeforeLeft = 0;
  // A television has no mouse, so the cursor is hidden until one moves -- the
  // same page is worked on over VNC, where clicking a tile has to be possible.
  const POINTER_IDLE_MS = 2500;
  let pointingTimer = null;
  // What the options screen is showing, and what it is waiting for.
  const CAPTURE_DONE = ["captured", "timeout", "cancelled", "error"];
  const OPTIONS = { rows: [], focus: 0, busy: "", loaded: false,
    open: new Set(["piper"]), capture: null, view: "list",
    remote: { rows: [], row: 0, col: 0 }, receiverSummary: "", receiverError: "" };
  // What a key says when it is drawn small and read from a sofa. Anything not
  // named here keeps the label the library gave it.
  const KEY_TEXT = {
    power: "⏻", up: "▲", down: "▼", left: "◀", right: "▶", ok: "OK",
    home: "⌂", back: "↩", exit: "EXIT", menu: "MENU", list: "LIST",
    info: "i", help: "?", guide: "GUIDE", favorites: "FAV",
    volume_up: "VOL +", volume_down: "VOL −", channel_up: "CH +",
    channel_down: "CH −", mute: "MUTE", source: "SRC",
    rewind: "◀◀", play: "▶", fast_forward: "▶▶", previous: "|◀",
    pause: "❚❚", next: "▶|", record: "●", stop: "■", three_d: "3D",
    tv_radio: "TV/RAD", audio: "AUDIO", format: "FORMAT",
  };
  let busyTimer = null;

  const unit = () => Math.min(window.innerWidth / 1920, window.innerHeight / 1080);

  function place(element, x, y, size) {
    // Positions are applied through the CSSOM: the page's policy forbids
    // style attributes in markup, and the ring is computed anyway.
    const u = unit();
    element.style.width = `${size * u}px`;
    element.style.height = `${size * u}px`;
    element.style.left = `${(x - size / 2) * u}px`;
    element.style.top = `${(y - size / 2) * u}px`;
    element.style.fontSize = `${size * 0.4 * u}px`;
  }

  function buildRing() {
    const ring = $("ring");
    ring.replaceChildren();
    state.tiles = SERVICES.map((service, index) => {
      const tile = document.createElement("div");
      tile.className = "tile";
      tile.dataset.id = service.id;
      tile.textContent = service.letter;
      if (service.colour) tile.style.background = service.colour;
      tile.setAttribute("role", "img");
      tile.setAttribute("aria-label", service.name);
      // A click chooses, and a click on what is already chosen opens it --
      // the same two steps the remote takes, so neither surprises the other.
      tile.addEventListener("click", () => {
        if (state.open) return;
        if (index !== state.focus) {
          state.focus = index;
          layoutRing();
          renderFocus();
          return;
        }
        press("ok");
      });
      ring.append(tile);
      return { element: tile, service, index };
    });
    layoutRing();
  }

  function layoutRing() {
    const count = state.tiles.length;
    state.tiles.forEach(({ element, service, index }) => {
      // Slot 0 sits at twelve o'clock and the rest run clockwise, matching the
      // reference where search occupies the top of the dial.
      const offset = index - state.focus;
      const angle = (offset / count) * Math.PI * 2 - Math.PI / 2;
      const focused = index === state.focus;
      const size = focused ? FOCUSED_TILE : TILE;
      const radius = focused ? 0 : RADIUS;
      place(element, CENTRE.x + Math.cos(angle) * radius,
            CENTRE.y + Math.sin(angle) * radius, size);
      element.classList.toggle("is-focused", focused);
      element.classList.toggle("is-empty", service.kind === "search");
    });
  }

  function ago(seconds) {
    if (!(seconds >= 0)) return "";
    const minutes = Math.round(seconds / 60);
    if (minutes < 2) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    return days <= 1 ? "yesterday" : `${days} days ago`;
  }

  function lasted(seconds) {
    const minutes = Math.round((seconds || 0) / 60);
    if (minutes < 1) return "under a minute";
    if (minutes < 60) return `${minutes}m`;
    return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
  }

  function renderHistory() {
    const list = $("history-list");
    const entries = (state.services && state.services.history) || [];
    list.replaceChildren();
    if (!entries.length) {
      const empty = document.createElement("li");
      empty.className = "history-item";
      const when = document.createElement("div");
      when.className = "history-when";
      when.textContent = "nothing opened yet";
      empty.append(when);
      list.append(empty);
      return;
    }
    for (const entry of entries) {
      const service = BY_ID.get(entry.id);
      const item = document.createElement("li");
      item.className = "history-item";
      const badge = document.createElement("span");
      badge.className = "history-badge";
      badge.textContent = service ? service.letter : "·";
      if (service && service.colour) badge.style.background = service.colour;
      const text = document.createElement("div");
      const name = document.createElement("div");
      name.className = "history-name";
      name.textContent = entry.name;
      const when = document.createElement("div");
      when.className = "history-when";
      when.textContent = `${ago(entry.age_s)} · ${lasted(entry.seconds)}`;
      text.append(name, when);
      item.append(badge, text);
      list.append(item);
    }
  }

  function openable(id) {
    const known = (state.services && state.services.services) || [];
    return known.some((service) => service.id === id);
  }

  function renderFocus() {
    const service = SERVICES[state.focus];
    $("focus-name").textContent = service.name;
    // The ring is the design's list of services; only some of them are ones
    // Piper can actually start, and the caption is where that is admitted.
    $("focus-note").textContent = service.kind === "search"
      ? "search is not implemented yet"
      : openable(service.id) ? "OK to open" : `piper cannot open ${service.name} yet`;
    $("ring-hint").textContent =
      `◀ ▶ choose · OK opens · ${state.focus + 1} of ${SERVICES.length}`;
  }

  function showScreen(name) {
    state.screen = name;
    $("screen-boot").classList.toggle("is-shown", name === "boot");
    $("screen-home").classList.toggle("is-shown", name === "home");
    $("screen-options").classList.toggle("is-shown", name === "options");
  }

  function notify(message, persist = false, kind = "") {
    clearTimeout(noticeTimer);
    const notice = $("tv-notice");
    notice.textContent = message || "";
    notice.hidden = !message;
    state.notice = message ? kind : "";
    if (message && !persist) noticeTimer = setTimeout(() => {
      notice.hidden = true;
      state.notice = "";
    }, 4000);
  }

  function clock() {
    const now = new Date();
    const time = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
    $("home-clock").textContent = time;
    $("options-clock").textContent = time;
    $("boot-meta").textContent = `raspberry pi · ${time}`;
    // "just now" becomes "20 min ago" without anything else having changed.
    if (state.screen === "home") renderHistory();
  }

  function move(step) {
    const count = SERVICES.length;
    state.focus = (state.focus + step + count) % count;
    layoutRing();
    renderFocus();
  }

  async function ask(path, payload, whenItFails) {
    try {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        // The server knows why -- no browser, wrong input, an earlier visit --
        // so its sentence is shown instead of a guess made here.
        notify(result.error || whenItFails);
        return null;
      }
      applyServices(result);
      return result;
    } catch {
      notify(`${whenItFails} The Pi did not answer.`);
      return null;
    }
  }

  async function get(path) {
    const response = await fetch(path, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`the Pi answered ${response.status}`);
    return response.json();
  }

  async function send(method, path, payload) {
    const response = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload || {}),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || `the Pi answered ${response.status}`);
    return result;
  }

  async function openService(service) {
    if (state.opening) return;
    state.opening = true;
    notify(`Opening ${service.name}…`, true, "opening");
    try {
      await ask("/api/tv/launch", { service: service.id, session_id: state.session },
                `Could not open ${service.name}.`);
    } finally {
      state.opening = false;
    }
  }

  // The remote's own way back is handled on the Pi, because this page is behind
  // the service's window. This covers a keyboard, and a second press does no harm.
  const closeService = () => ask("/api/tv/close", {}, "Could not close the open service.");

  // The mouse's way out of Piper. The Pi closes this page in answering, and
  // the remote goes on to move the desktop's mouse.
  function leavePiper() {
    send("POST", "/api/tv/leave", {}).catch((error) => notify(`Could not leave Piper. ${error.message}`));
  }

  function press(button) {
    if (state.screen === "options") { pressOptions(button); return; }
    if (state.screen !== "home") return;
    if (state.open) {
      // Something owns the screen; the ring must not move behind it, and back
      // belongs to that service. Only exit and home come back here.
      if (button === "exit" || button === "home") closeService();
      return;
    }
    switch (button) {
      case "right": move(1); break;
      case "left": {
        const now = Date.now();
        if (now - lastLeftAt <= DOUBLE_LEFT_MS) {
          // The gesture, not two steps: put the wheel back where it was and
          // open the options instead.
          lastLeftAt = 0;
          state.focus = focusBeforeLeft;
          layoutRing();
          renderFocus();
          openOptions();
          break;
        }
        lastLeftAt = now;
        focusBeforeLeft = state.focus;
        move(-1);
        break;
      }
      case "down": move(1); break;
      case "up": move(-1); break;
      case "ok": {
        const service = SERVICES[state.focus];
        if (service.kind === "search") {
          notify("Search is not implemented yet.");
          break;
        }
        openService(service);
        break;
      }
      case "home": move(-state.focus); break;
      default: break;
    }
  }

  // --- the options: the remote itself, and the shape of the screen --------

  // Sections, so the whole remote fits on a television screen: everything is
  // here, and only what was asked for is open. Nine presses reach the last of
  // them, which is the point of collapsing them in the first place.
  const SECTION_TITLES = {
    receiver: "ir receiver",
    piper: "what piper does", power: "power", numbers: "numbers",
    colors: "colour keys", navigation: "navigation", controls: "volume & channels",
    apps: "apps", playback: "playback", teletext: "picture & sound",
  };
  const VISIBLE_ROWS = 13;

  function renderCurrent() {
    if (OPTIONS.view === "remote") renderRemote();
    else renderOptions();
  }

  function say(message, forSeconds = 0) {
    clearTimeout(busyTimer);
    OPTIONS.busy = message;
    renderCurrent();
    if (message && forSeconds) {
      busyTimer = setTimeout(() => { OPTIONS.busy = ""; renderCurrent(); }, forSeconds * 1000);
    }
  }

  function countSamples(recordings, id) {
    const record = recordings[id];
    return record && record.samples ? record.samples.length : 0;
  }

  async function refreshOptions() {
    // Three things the Pi knows: which button carries each of Piper's roles,
    // what the remote's buttons are and which of them have been recorded, and
    // whether Piper is filling the screen. Read together, so the list is
    // never half true.
    const [roles, library, shape, receiver] = await Promise.all([
      get("/api/roles"), get("/api/state"), get("/api/window"),
      // Optional: a server without GPIO still has everything else to offer.
      get("/api/receiver").catch(() => null),
    ]);
    const recordings = library.recordings || {};
    const buttons = library.buttons || [];
    const labels = new Map(buttons.map((button) => [button.id, button.label]));
    const rows = [{ kind: "window", settings: shape.settings, section: null },
                  { kind: "remote", section: null }];
    if (receiver) rows.push(...receiverRows(receiver));

    rows.push({ kind: "section", section: "piper" });
    for (const entry of roles.roles || []) {
      rows.push({
        kind: "role", section: "piper", role: entry.role, button: entry.button,
        buttonLabel: labels.get(entry.button) || entry.button,
        rebound: !!entry.rebound, samples: countSamples(recordings, entry.button),
      });
    }

    const sections = [];
    for (const button of buttons) {
      if (!sections.includes(button.section)) sections.push(button.section);
    }
    for (const section of sections) {
      rows.push({ kind: "section", section });
      for (const button of buttons.filter((entry) => entry.section === section)) {
        rows.push({
          kind: "button", section, button: button.id, buttonLabel: button.label,
          samples: countSamples(recordings, button.id),
        });
      }
    }
    OPTIONS.rows = rows;
    OPTIONS.remote.rows = buildRemote(buttons, recordings);
    OPTIONS.loaded = true;
    OPTIONS.focus = Math.min(OPTIONS.focus, Math.max(0, visibleRows().length - 1));
    renderCurrent();
  }

  function receiverRows(receiver) {
    // Which pin the IR receiver's OUT wire is on -- the same setting as the
    // pin line in pipertv.conf. Piper works out how to read it: through the
    // kernel's receiver when that already holds the pin, directly otherwise.
    const pin = receiver.pin;
    const heard = receiver.listening;
    const kernel = receiver.kernel || {};
    const lines = receiver.lines || [];
    const onHeader = (gpio) => (lines.find((line) => line.gpio === gpio) || {}).header_pin;
    OPTIONS.receiverSummary = (pin === "auto" ? "auto" : `GPIO${pin}`)
      + (heard && heard.error ? " · not listening" : "");
    OPTIONS.receiverError = heard && heard.error ? heard.error : "";
    const automatic = kernel.gpio != null
      ? `kernel receiver · GPIO${kernel.gpio}`
      : `GPIO${receiver.default_pin} · pin ${onHeader(receiver.default_pin) || "?"}`;
    const rows = [{ kind: "section", section: "receiver" }, {
      kind: "receiver", section: "receiver", choice: "auto", free: true,
      label: "automatic", value: automatic, current: pin === "auto",
    }];
    // Plain pins first: the ones with no second name on the Pi's pinout, which
    // nothing added later will want back. A pin that also has a job of its
    // own (SDA, TXD, PCM_CLK…) says so, as the pinout does.
    const ordered = [...lines].sort((a, b) =>
      (a.function ? 1 : 0) - (b.function ? 1 : 0) || a.gpio - b.gpio);
    for (const line of ordered) {
      const notes = [`pin ${line.header_pin}`];
      if (line.function) notes.push(line.function);
      if (line.kernel) notes.push("kernel receiver");
      else if (!line.free) notes.push(`used by ${line.consumer || "another driver"}`);
      rows.push({
        kind: "receiver", section: "receiver", choice: line.gpio, free: line.free,
        label: `GPIO${line.gpio}`, pin: line.header_pin, viaKernel: !!line.kernel,
        holder: line.consumer || "another driver",
        function: line.function, purpose: line.purpose,
        value: notes.join(" · "), current: pin === line.gpio,
      });
    }
    return rows;
  }

  async function chooseReceiver(row) {
    if (row.current) { say("the remote is already read here", 3); return; }
    if (!row.free) { say(`${row.label} is in use · choose a free pin`, 4); return; }
    say(`listening on ${row.label}…`);
    try {
      const result = await send("PUT", "/api/receiver", { pin: row.choice });
      const heard = result.listening;
      say(heard && heard.error ? `not listening: ${heard.error}`
        : `reading ${result.reading} · press a button on the remote to try it`, 6);
    } catch (error) {
      say(error.message, 6);
    }
    refreshOptions().catch(() => {});
  }

  function visibleRows() {
    // A section's own row is always there, and so is anything that belongs to
    // no section; what is under a section is there once it has been opened.
    return OPTIONS.rows.filter((row) => row.section === null || row.kind === "section"
      || OPTIONS.open.has(row.section));
  }

  function optionTitle(row) {
    if (row.kind === "window") return "Display";
    if (row.kind === "remote") return "The remote";
    if (row.kind === "section") {
      return `${OPTIONS.open.has(row.section) ? "▾" : "▸"} ${SECTION_TITLES[row.section] || row.section}`;
    }
    if (row.kind === "receiver") return row.label;
    return row.kind === "role" ? row.role : row.buttonLabel;
  }

  function optionValue(row) {
    if (row.kind === "window") {
      return row.settings.windowed
        ? `window · ${row.settings.width}×${row.settings.height}` : "full screen";
    }
    if (row.kind === "remote") {
      const keys = OPTIONS.remote.rows.reduce((total, line) => total + line.keys.length, 0);
      return `drawn · ${keys} keys`;
    }
    if (row.kind === "section") return row.section === "receiver" ? OPTIONS.receiverSummary : "";
    if (row.kind === "receiver") return `${row.value}${row.current ? " · ● in use" : ""}`;
    const recorded = row.samples
      ? `${row.samples} ${row.samples === 1 ? "recording" : "recordings"}` : "not recorded";
    if (row.kind === "role") {
      return `${row.buttonLabel}${row.rebound ? " (moved)" : ""} · ${recorded}`;
    }
    return recorded;
  }

  function optionNote(row) {
    if (row.kind === "window") {
      return row.settings.windowed
        ? "OK fills the screen again · piper restarts"
        : "OK puts piper in a window, with the desktop around it · piper restarts";
    }
    if (row.kind === "remote") {
      return "OK draws the remote itself · pick a key with the arrows and record it";
    }
    if (row.kind === "section") {
      if (row.section === "receiver" && OPTIONS.receiverError) return OPTIONS.receiverError;
      return OPTIONS.open.has(row.section) ? "OK closes this group" : "OK opens this group";
    }
    if (row.kind === "receiver") {
      if (row.current) return "the remote is read here now · kept in pipertv.conf";
      if (!row.free) return `${row.label} is taken by ${row.holder} · choose another pin`;
      if (row.choice === "auto") {
        return "OK finds the receiver by itself: the kernel's, if config.txt sets one up,"
          + " otherwise GPIO17 · kept in pipertv.conf";
      }
      if (row.viaKernel) {
        return `OK reads ${row.label} through the kernel's receiver, which already holds it`;
      }
      if (row.function) {
        return `${row.label} (pin ${row.pin}) is also ${row.function}, for ${row.purpose}.`
          + " It works as the receiver's input while that is off · a pin with no second"
          + " name is the safer choice";
      }
      return `OK reads the remote from ${row.label} from now on · the receiver's OUT goes to`
        + ` pin ${row.pin} · kept in pipertv.conf`;
    }
    const what = row.kind === "role" ? `the ${row.buttonLabel} button, which piper uses for ${row.role},` : `the ${row.buttonLabel} button`;
    return row.samples
      ? `OK records ${what} again · the old recording is kept as well`
      : `OK records ${what} · it has no signal yet`;
  }

  function renderOptions() {
    const rows = visibleRows();
    const list = $("options-list");
    list.replaceChildren();
    // Only a screenful is drawn, and the chosen row stays inside it.
    const last = Math.max(0, rows.length - VISIBLE_ROWS);
    const start = Math.max(0, Math.min(OPTIONS.focus - Math.floor(VISIBLE_ROWS / 2), last));
    rows.slice(start, start + VISIBLE_ROWS).forEach((row, offset) => {
      const index = start + offset;
      const item = document.createElement("li");
      item.className = `option option-${row.kind}${index === OPTIONS.focus ? " is-focused" : ""}`;
      const label = document.createElement("span");
      label.className = "option-label";
      label.textContent = optionTitle(row);
      const value = document.createElement("span");
      value.className = "option-value";
      value.textContent = optionValue(row);
      item.append(label, value);
      // One click does it. A second click would land somewhere else anyway:
      // choosing a row scrolls the list under the cursor.
      item.addEventListener("click", () => {
        OPTIONS.focus = index;
        activateOption();
      });
      list.append(item);
    });
    const row = rows[OPTIONS.focus];
    $("option-name").textContent = row ? optionTitle(row).replace(/^[▾▸] /, "") : "";
    $("option-note").textContent = row ? optionNote(row) : "reading the library…";
    const busy = $("option-busy");
    busy.textContent = OPTIONS.busy;
    busy.hidden = !OPTIONS.busy;
    $("options-count").textContent = rows.length
      ? `${OPTIONS.focus + 1} of ${rows.length}` : "";
    $("options-hint").textContent = OPTIONS.capture
      ? "press the button on your remote · OK or back cancels"
      : "▲ ▼ choose · OK · ▶ back to the wheel · ▲ at the top does the same";
  }

  function keyText(button) {
    if (KEY_TEXT[button.id]) return KEY_TEXT[button.id];
    if (button.id.startsWith("digit_")) return button.id.slice(6);
    return button.label;
  }

  function buildRemote(buttons, recordings) {
    // The rows are the library's own: the arrangement is a property of the
    // remote, not of this page, so the drawing cannot drift from the thing.
    const rows = [];
    for (const button of buttons) {
      const index = rows.findIndex((row) => row.number === button.row);
      const entry = { ...button, samples: countSamples(recordings, button.id) };
      if (index < 0) rows.push({ number: button.row, keys: [entry] });
      else rows[index].keys.push(entry);
    }
    for (const row of rows) row.keys.sort((one, other) => one.col - other.col);
    return rows;
  }

  function focusedKey() {
    const row = OPTIONS.remote.rows[OPTIONS.remote.row];
    return row ? row.keys[Math.min(OPTIONS.remote.col, row.keys.length - 1)] : null;
  }

  function moveKey(dx, dy) {
    const remote = OPTIONS.remote;
    const rows = remote.rows;
    if (!rows.length) return;
    if (dx) {
      const keys = rows[remote.row].keys;
      remote.col = Math.max(0, Math.min(keys.length - 1, remote.col + dx));
      renderRemote();
      return;
    }
    // Between rows, the key nearest the same place across the width: down
    // from the up arrow is OK, not whatever happens to be first in the row.
    const here = rows[remote.row].keys;
    const place = (Math.min(remote.col, here.length - 1) + 0.5) / here.length;
    const next = Math.max(0, Math.min(rows.length - 1, remote.row + dy));
    const keys = rows[next].keys;
    let best = 0;
    keys.forEach((_key, index) => {
      const distance = Math.abs((index + 0.5) / keys.length - place);
      if (distance < Math.abs((best + 0.5) / keys.length - place)) best = index;
    });
    remote.row = next;
    remote.col = best;
    renderRemote();
  }

  function renderRemote() {
    const body = $("remote-body");
    body.replaceChildren();
    OPTIONS.remote.rows.forEach((row, rowIndex) => {
      const line = document.createElement("div");
      line.className = "key-row";
      row.keys.forEach((button, colIndex) => {
        const key = document.createElement("button");
        key.type = "button";
        const chosen = rowIndex === OPTIONS.remote.row
          && colIndex === Math.min(OPTIONS.remote.col, row.keys.length - 1);
        key.className = `remote-key ${button.id}`
          + (button.section === "colors" ? " is-colour" : "")
          + (button.samples ? " is-saved" : "")
          + (chosen ? " is-focused" : "");
        key.textContent = keyText(button);
        key.setAttribute("aria-label",
          `${button.label}${button.samples ? `, ${button.samples} recorded` : ", not recorded"}`);
        key.addEventListener("click", () => {
          OPTIONS.remote.row = rowIndex;
          OPTIONS.remote.col = colIndex;
          renderRemote();
          learn({ button: button.id, buttonLabel: button.label });
        });
        line.append(key);
      });
      body.append(line);
    });
    const button = focusedKey();
    $("option-name").textContent = button ? button.label : "";
    $("option-note").textContent = button
      ? (button.samples
         ? `OK records it again · ${button.samples} already saved`
         : "OK records it · it has no signal yet")
      : "";
    const busy = $("option-busy");
    busy.textContent = OPTIONS.busy;
    busy.hidden = !OPTIONS.busy;
    $("options-count").textContent = button ? button.id.replace(/_/g, " ") : "";
    $("options-hint").textContent = OPTIONS.capture
      ? "press the button on your remote · OK or back cancels"
      : "▲ ▼ ◀ ▶ choose a key · OK records it · back returns to the list";
  }

  function showRemote(show) {
    OPTIONS.view = show ? "remote" : "list";
    $("options-list").hidden = show;
    $("remote-body").hidden = !show;
    $("options-back").textContent = show ? "◀ back to the list" : "◀ back to the wheel";
    if (show) renderRemote();
    else renderOptions();
  }

  function pressRemote(button) {
    switch (button) {
      case "up": moveKey(0, -1); break;
      case "down": moveKey(0, 1); break;
      case "left": moveKey(-1, 0); break;
      case "right": moveKey(1, 0); break;
      case "ok": {
        if (OPTIONS.capture) { cancelCapture(); break; }
        const key = focusedKey();
        if (key) learn({ button: key.id, buttonLabel: key.label });
        break;
      }
      case "back": case "home":
        if (OPTIONS.capture) cancelCapture();
        else showRemote(false);
        break;
      default: break;
    }
  }

  function wheelOptions(event) {
    // A list this long is worth a wheel when there is a mouse on the desk.
    if (state.screen !== "options") return;
    event.preventDefault();
    moveOption(event.deltaY > 0 ? 1 : -1);
  }

  function moveOption(step) {
    const count = visibleRows().length;
    if (!count) return;
    OPTIONS.focus = (OPTIONS.focus + step + count) % count;
    renderOptions();
  }

  async function openOptions() {
    showScreen("options");
    showRemote(false);
    say("");
    try {
      await refreshOptions();
    } catch (error) {
      say(`the options could not be read · ${error.message || error}`, 6);
    }
  }

  function closeOptions() {
    if (OPTIONS.capture) { cancelCapture(); return; }
    if (OPTIONS.view === "remote") { showRemote(false); return; }
    clearTimeout(busyTimer);
    OPTIONS.busy = "";
    showScreen("home");
    layoutRing();
    renderFocus();
  }

  async function waitForCapture(id) {
    // The gate is shut while a signal is being learned, so the press being
    // recorded does not also drive this page. Nothing arrives here until the
    // Pi is finished with it either way.
    for (let attempt = 0; attempt < 480; attempt += 1) {
      const job = await get(`/api/captures/${id}`);
      if (CAPTURE_DONE.includes(job.status)) return job;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return { status: "timeout" };
  }

  function outcome(job) {
    if (job.status === "captured") return "saved · that button works again";
    if (job.status === "timeout") return "nothing arrived · point the remote at the receiver and try again";
    if (job.status === "cancelled") return "cancelled";
    return job.error || "the recording failed";
  }

  async function cancelCapture() {
    const id = OPTIONS.capture;
    if (!id) return;
    OPTIONS.capture = null;
    try {
      await send("POST", `/api/captures/${id}/cancel`, {});
    } catch {
      // It finished on its own between the press and this request.
    }
    say("cancelled", 4);
  }

  async function learn(row) {
    say(`press the ${row.buttonLabel} button on your remote now`);
    let job;
    try {
      job = await send("POST", "/api/captures", { button_id: row.button });
    } catch (error) {
      say(String(error.message || error), 8);
      return;
    }
    OPTIONS.capture = job.id;
    try {
      const done = await waitForCapture(job.id);
      OPTIONS.capture = null;
      say(outcome(done), 8);
    } catch (error) {
      OPTIONS.capture = null;
      say(String(error.message || error), 8);
      return;
    }
    try {
      await refreshOptions();
    } catch {
      // The list is stale rather than wrong; the next visit reads it again.
    }
  }

  async function switchWindow(row) {
    const windowed = !row.settings.windowed;
    say(windowed ? "putting piper in a window…" : "filling the screen again…");
    try {
      await send("PUT", "/api/window", { windowed });
    } catch {
      // Expected as often as not: this page is the window being replaced, so
      // the answer has nowhere to arrive. What follows is a new page.
    }
  }

  function activateOption() {
    if (OPTIONS.capture) { cancelCapture(); return; }
    const row = visibleRows()[OPTIONS.focus];
    if (!row) return;
    if (row.kind === "window") { switchWindow(row); return; }
    if (row.kind === "remote") { showRemote(true); return; }
    if (row.kind === "receiver") { chooseReceiver(row); return; }
    if (row.kind === "section") {
      if (OPTIONS.open.has(row.section)) OPTIONS.open.delete(row.section);
      else OPTIONS.open.add(row.section);
      renderOptions();
      return;
    }
    learn(row);
  }

  function pressOptions(button) {
    if (OPTIONS.view === "remote") { pressRemote(button); return; }
    switch (button) {
      case "down": moveOption(1); break;
      case "up":
        if (OPTIONS.capture) break;   // a recording is waiting for a press
        if (OPTIONS.focus === 0) closeOptions();
        else moveOption(-1);
        break;
      case "right": case "back": case "home": closeOptions(); break;
      case "ok": activateOption(); break;
      default: break;
    }
  }

  function applyLeaving(leaving) {
    // Asked on the Pi, not here: this page may be behind a service's window,
    // and the question has to survive the gate being shut.
    const armed = !!(leaving && leaving.armed);
    if (armed && state.notice !== "leaving") {
      notify("press exit again to close piper", true, "leaving");
    } else if (!armed && state.notice === "leaving") {
      notify("");
    }
  }

  function signature(services) {
    // Everything except the clocks: ages and uptimes change on every poll and
    // must not redraw the page four times a second.
    if (!services) return "";
    const known = (services.services || []).map((service) => service.id).join(",");
    const history = (services.history || [])
      .map((entry) => `${entry.id}@${entry.ended_at}`).join(",");
    return [services.running ? services.running.id : "", known, history,
            services.error || "", services.available].join("|");
  }

  function applyServices(services) {
    const changed = signature(services) !== signature(state.services);
    state.services = services || null;
    const running = (services && services.running) || null;
    if (running && state.open !== running.id) {
      notify(`${running.name} is open · exit returns to piper`, true, "open");
    } else if (!running && (state.notice === "open"
               || (state.notice === "opening" && !state.opening))) {
      // Either it closed, or it never came up: neither leaves a notice standing.
      notify("");
    }
    state.open = running ? running.id : null;
    const failure = (services && services.error) || null;
    if (failure && failure !== state.serviceError) notify(failure);
    state.serviceError = failure;
    if (changed) {
      renderHistory();
      renderFocus();
    }
  }

  document.addEventListener("keydown", (event) => {
    const button = KEYS[event.key];
    if (!button) return;
    event.preventDefault();
    press(button);
  });

  window.addEventListener("resize", () => { if (state.screen === "home") layoutRing(); });

  // A mouse, when there is one: the cursor appears while it moves and goes
  // away again, so a television is not left with an arrow parked on it.
  document.addEventListener("mousemove", () => {
    document.body.classList.add("is-pointing");
    clearTimeout(pointingTimer);
    pointingTimer = setTimeout(
      () => document.body.classList.remove("is-pointing"), POINTER_IDLE_MS);
  });

  async function poll() {
    let delay = 250;
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 3000);
    try {
      const response = await fetch(`/api/tv/events?after=${state.seen}`, {
        headers: { Accept: "application/json" },
        signal: abort.signal,
      });
      if (response.status === 404) {
        // Started without desktop control: the page still works by keyboard.
        notify("This server is running without remote control. Use a keyboard.", true);
        state.primed = false;
        delay = 5000;
      } else if (!response.ok) {
        state.primed = false;
        delay = 2000;
      } else {
        const feed = await response.json();
        // A new page/reconnection establishes a cursor; it must never replay
        // retained OK presses. A stream id detects restarts even if the new
        // server's sequence has already overtaken our previous number.
        const continuous = state.primed && state.stream === feed.stream_id && !feed.missed;
        if (feed.missed) notify("Some presses were missed.");
        state.seen = feed.sequence;
        state.stream = feed.stream_id;
        state.session = feed.session_id || null;
        applyServices(feed.services);
        applyLeaving(feed.leaving);
        state.primed = (state.screen === "home" || state.screen === "options")
          && !document.hidden;
        state.control = feed.control;
        const source = feed.control === "on"
          ? feed.mode === "piper" ? "remote connected" : "desktop control selected"
          : "remote control off";
        $("home-source").textContent = source;
        $("options-state").textContent = source;
        if (continuous && state.primed && feed.control === "on" && feed.mode === "piper") {
          for (const event of feed.events || []) {
            if (event.mode === "piper" && event.session_id === feed.session_id && event.navigation) {
              // What the key performs, not which key it was: a role bound to a
              // button the TV ignores arrives under that button's name.
              press(event.action || event.button);
            }
          }
        }
      }
    } catch {
      state.primed = false;
      $("home-source").textContent = "server unavailable";
      delay = 3000;
    } finally {
      clearTimeout(timeout);
    }
    setTimeout(poll, delay);
  }

  document.addEventListener("visibilitychange", () => { state.primed = false; });

  function initialize() {
    const params = new URLSearchParams(window.location.search);
    // Piper closes this page while a service is on the screen and opens it
    // again afterwards; it comes back on the tile that was just closed, as if
    // it had been waiting behind it the whole time.
    const returning = SERVICES.findIndex((service) => service.id === params.get("focus"));
    if (returning >= 0) state.focus = returning;
    renderHistory();
    buildRing();
    renderFocus();
    $("options-corner").addEventListener("click", openOptions);
    $("options-back").addEventListener("click", closeOptions);
    $("home-exit").addEventListener("click", leavePiper);
    $("options-list").addEventListener("wheel", wheelOptions, { passive: false });
    clock();
    setInterval(clock, 20000);

    // A kiosk that reloads should not replay the splash every time, and it is
    // the only way to look at the dial in a renderer that cannot wait.
    const skipBoot = params.get("boot") === "0";
    $("boot-fill").style.width = "62%";
    $("boot-status").textContent = "starting · reading the remote";
    if (skipBoot) {
      showScreen("home");
      layoutRing();
    } else {
      setTimeout(() => {
        $("boot-fill").style.width = "100%";
        showScreen("home");
        layoutRing();
      }, 1400);
    }

    poll();
  }

  initialize();
})();
