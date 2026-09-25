"use strict";

/* The Piper interface on the TV.
 *
 * Remote presses are polled from /api/tv/events rather than injected as key
 * events, so the page works the same with a keyboard during development.
 * Which tiles can actually be opened is up to the server.
 */

(() => {
  const $ = (id) => document.getElementById(id);

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

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

  // The dial, in 1920x1080 design pixels (scaled by unit()).
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

  // Left pressed twice quickly opens the options (and undoes the first step).
  const DOUBLE_LEFT_MS = 450;
  let lastLeftAt = 0;
  let focusBeforeLeft = 0;

  // The mouse cursor is hidden unless a mouse is actually moving (e.g. over VNC).
  const POINTER_IDLE_MS = 2500;
  let pointingTimer = null;

  const CAPTURE_DONE = ["captured", "timeout", "cancelled", "error"];
  const OPTIONS = { rows: [], focus: 0, busy: "", loaded: false,
    open: new Set(["piper"]), capture: null, view: "list",
    remote: { rows: [], row: 0, col: 0 }, receiverSummary: "", receiverError: "" };
  let busyTimer = null;

  // Short labels for the drawn remote; other keys use their library label.
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

  // --- requests ----------------------------------------------------------

  async function api(method, path, payload) {
    const init = { method, headers: { Accept: "application/json" } };
    if (payload !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(payload);
    }
    const response = await fetch(path, init);
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(result.error || `the Pi answered ${response.status}`);
      error.reason = result.error;
      throw error;
    }
    return result;
  }

  const get = (path) => api("GET", path);
  const send = (method, path, payload) => api(method, path, payload || {});

  // A request made from the dial; a failure is shown as a notice. The
  // server's own message is used when there is one, since it knows why.
  async function ask(path, payload, whenItFails) {
    try {
      const result = await send("POST", path, payload);
      applyServices(result);
      return result;
    } catch (error) {
      // fetch() rejects with a TypeError when the request never got an answer.
      notify(error instanceof TypeError ? `${whenItFails} The Pi did not answer.`
        : error.reason || whenItFails);
      return null;
    }
  }

  // --- the dial ----------------------------------------------------------

  const unit = () => Math.min(window.innerWidth / 1920, window.innerHeight / 1080);

  function place(element, x, y, size) {
    // Set through the CSSOM: the CSP forbids style attributes.
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
      const tile = el("div", "tile", service.letter);
      tile.dataset.id = service.id;
      if (service.colour) tile.style.background = service.colour;
      tile.setAttribute("role", "img");
      tile.setAttribute("aria-label", service.name);
      // Click selects; clicking the selected tile opens it, like OK.
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

  // How far a tile sits from the selected one, the short way round. Plain
  // subtraction would say eight steps where the wheel only turns one, and the
  // whole ring would slide backwards on passing the last tile.
  function ringOffset(index, count) {
    const straight = index - state.focus;
    return straight - count * Math.round(straight / count);
  }

  function layoutRing() {
    const count = state.tiles.length;
    state.tiles.forEach((tile) => {
      const { element, service, index } = tile;
      const offset = ringOffset(index, count);
      // One tile is always on the far side, where the short way round changes
      // sides. It is moved without animating, or it would slide across the
      // middle of the wheel while the others step round it.
      const crossed = tile.offset !== undefined && Math.abs(offset - tile.offset) > 1;
      tile.offset = offset;
      if (crossed) element.style.transition = "none";
      // The selected tile sits in the middle, the rest clockwise from twelve o'clock.
      const angle = (offset / count) * Math.PI * 2 - Math.PI / 2;
      const focused = index === state.focus;
      const radius = focused ? 0 : RADIUS;
      place(element, CENTRE.x + Math.cos(angle) * radius,
            CENTRE.y + Math.sin(angle) * radius, focused ? FOCUSED_TILE : TILE);
      element.classList.toggle("is-focused", focused);
      element.classList.toggle("is-empty", service.kind === "search");
      if (crossed) {
        void element.offsetWidth;    // let the move land before it can animate
        element.style.transition = "";
      }
    });
  }

  function move(step) {
    const count = SERVICES.length;
    state.focus = (state.focus + step + count) % count;
    layoutRing();
    renderFocus();
  }

  function openable(id) {
    const known = (state.services && state.services.services) || [];
    return known.some((service) => service.id === id);
  }

  function renderFocus() {
    const service = SERVICES[state.focus];
    $("focus-name").textContent = service.name;
    $("focus-note").textContent = service.kind === "search"
      ? "search is not implemented yet"
      : openable(service.id) ? "OK to open" : `piper cannot open ${service.name} yet`;
    $("ring-hint").textContent =
      `◀ ▶ choose · OK opens · ${state.focus + 1} of ${SERVICES.length}`;
  }

  // --- launch history ----------------------------------------------------

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
      const empty = el("li", "history-item");
      empty.append(el("div", "history-when", "nothing opened yet"));
      list.append(empty);
      return;
    }
    for (const entry of entries) {
      const service = BY_ID.get(entry.id);
      const badge = el("span", "history-badge", service ? service.letter : "·");
      if (service && service.colour) badge.style.background = service.colour;
      const text = el("div");
      text.append(el("div", "history-name", entry.name),
                  el("div", "history-when", `${ago(entry.age_s)} · ${lasted(entry.seconds)}`));
      const item = el("li", "history-item");
      item.append(badge, text);
      list.append(item);
    }
  }

  // --- screens and notices -------------------------------------------------

  function showScreen(name) {
    state.screen = name;
    for (const screen of ["boot", "home", "options"]) {
      $(`screen-${screen}`).classList.toggle("is-shown", name === screen);
    }
  }

  // `kind` tags a notice so it can be taken down when its reason goes away.
  function notify(message, persist = false, kind = "") {
    clearTimeout(noticeTimer);
    const notice = $("tv-notice");
    notice.textContent = message || "";
    notice.hidden = !message;
    state.notice = message ? kind : "";
    if (message && !persist) {
      noticeTimer = setTimeout(() => {
        notice.hidden = true;
        state.notice = "";
      }, 4000);
    }
  }

  function updateClock() {
    const now = new Date();
    const time = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
    $("home-clock").textContent = time;
    $("options-clock").textContent = time;
    $("boot-meta").textContent = `raspberry pi · ${time}`;
    if (state.screen === "home") renderHistory();  // "just now" becomes "20 min ago"
  }

  // --- services ------------------------------------------------------------

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

  // The remote's exit is handled on the Pi (this page is behind the service's
  // window); this is for a keyboard.
  const closeService = () => ask("/api/tv/close", {}, "Could not close the open service.");

  // The exit button, for a mouse. The Pi closes this page and the remote
  // goes on to move the desktop's mouse.
  function leavePiper() {
    send("POST", "/api/tv/leave").catch((error) => notify(`Could not leave Piper. ${error.message}`));
  }

  function press(button) {
    if (state.screen === "options") { pressOptions(button); return; }
    if (state.screen !== "home") return;
    if (state.open) {
      // A service is on the screen: the dial must not move behind it.
      if (button === "exit" || button === "home") closeService();
      return;
    }
    switch (button) {
      case "right": case "down": move(1); break;
      case "up": move(-1); break;
      case "left": {
        const now = Date.now();
        if (now - lastLeftAt <= DOUBLE_LEFT_MS) {
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
      case "ok": {
        const service = SERVICES[state.focus];
        if (service.kind === "search") notify("Search is not implemented yet.");
        else openService(service);
        break;
      }
      case "home": move(-state.focus); break;
      default: break;
    }
  }

  // --- options ---------------------------------------------------------------

  // The list is in collapsible sections so it fits on one screen.
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

  // The status line under the detail pane; cleared after `forSeconds` if given.
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
    const [roles, library, shape, receiver] = await Promise.all([
      get("/api/roles"), get("/api/state"), get("/api/window"),
      get("/api/receiver").catch(() => null),  // a server without GPIO has no pins to offer
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

    const sections = [...new Set(buttons.map((button) => button.section))];
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

  // Which pin the IR receiver's OUT wire is on (the same setting as in pipertv.conf).
  function receiverRows(receiver) {
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
    // Plain pins first, then those with a second function (SDA, TXD, PCM_CLK...).
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

  // Section rows are always visible; their contents only when the section is open.
  function visibleRows() {
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
    const what = row.kind === "role"
      ? `the ${row.buttonLabel} button, which piper uses for ${row.role},`
      : `the ${row.buttonLabel} button`;
    return row.samples
      ? `OK records ${what} again · the old recording is kept as well`
      : `OK records ${what} · it has no signal yet`;
  }

  function renderDetail(name, note, count, hint) {
    $("option-name").textContent = name;
    $("option-note").textContent = note;
    const busy = $("option-busy");
    busy.textContent = OPTIONS.busy;
    busy.hidden = !OPTIONS.busy;
    $("options-count").textContent = count;
    $("options-hint").textContent = OPTIONS.capture
      ? "press the button on your remote · OK or back cancels" : hint;
  }

  function renderOptions() {
    const rows = visibleRows();
    const list = $("options-list");
    list.replaceChildren();
    // Draw one screenful, keeping the selected row inside it.
    const last = Math.max(0, rows.length - VISIBLE_ROWS);
    const start = Math.max(0, Math.min(OPTIONS.focus - Math.floor(VISIBLE_ROWS / 2), last));
    rows.slice(start, start + VISIBLE_ROWS).forEach((row, offset) => {
      const index = start + offset;
      const item = el("li", `option option-${row.kind}${index === OPTIONS.focus ? " is-focused" : ""}`);
      item.append(el("span", "option-label", optionTitle(row)),
                  el("span", "option-value", optionValue(row)));
      // One click selects and activates: the list scrolls under the cursor
      // anyway, so a second click would land on another row.
      item.addEventListener("click", () => {
        OPTIONS.focus = index;
        activateOption();
      });
      list.append(item);
    });
    const row = rows[OPTIONS.focus];
    renderDetail(row ? optionTitle(row).replace(/^[▾▸] /, "") : "",
                 row ? optionNote(row) : "reading the library…",
                 rows.length ? `${OPTIONS.focus + 1} of ${rows.length}` : "",
                 "▲ ▼ choose · OK · ▶ back to the wheel · ▲ at the top does the same");
  }

  // --- the drawn remote --------------------------------------------------------

  function keyText(button) {
    if (KEY_TEXT[button.id]) return KEY_TEXT[button.id];
    if (button.id.startsWith("digit_")) return button.id.slice(6);
    return button.label;
  }

  // Rows as the library lays them out, so the drawing matches the real remote.
  function buildRemote(buttons, recordings) {
    const rows = [];
    for (const button of buttons) {
      const entry = { ...button, samples: countSamples(recordings, button.id) };
      const row = rows.find((line) => line.number === button.row);
      if (row) row.keys.push(entry);
      else rows.push({ number: button.row, keys: [entry] });
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
    // Moving between rows, pick the key at the nearest horizontal position:
    // down from the up arrow is OK, not the first key of the row.
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
      const line = el("div", "key-row");
      row.keys.forEach((button, colIndex) => {
        const chosen = rowIndex === OPTIONS.remote.row
          && colIndex === Math.min(OPTIONS.remote.col, row.keys.length - 1);
        const key = el("button", `remote-key ${button.id}`
          + (button.section === "colors" ? " is-colour" : "")
          + (button.samples ? " is-saved" : "")
          + (chosen ? " is-focused" : ""), keyText(button));
        key.type = "button";
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
    let note = "";
    if (button) {
      note = button.samples
        ? `OK records it again · ${button.samples} already saved`
        : "OK records it · it has no signal yet";
    }
    renderDetail(button ? button.label : "", note,
                 button ? button.id.replace(/_/g, " ") : "",
                 "▲ ▼ ◀ ▶ choose a key · OK records it · back returns to the list");
  }

  function showRemote(show) {
    OPTIONS.view = show ? "remote" : "list";
    $("options-list").hidden = show;
    $("remote-body").hidden = !show;
    $("options-back").textContent = show ? "◀ back to the list" : "◀ back to the wheel";
    renderCurrent();
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

  // --- moving around the options -------------------------------------------------

  function wheelOptions(event) {
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

  function activateOption() {
    if (OPTIONS.capture) { cancelCapture(); return; }
    const row = visibleRows()[OPTIONS.focus];
    if (!row) return;
    if (row.kind === "window") switchWindow(row);
    else if (row.kind === "remote") showRemote(true);
    else if (row.kind === "receiver") chooseReceiver(row);
    else if (row.kind === "section") {
      if (OPTIONS.open.has(row.section)) OPTIONS.open.delete(row.section);
      else OPTIONS.open.add(row.section);
      renderOptions();
    } else learn(row);
  }

  function pressOptions(button) {
    if (OPTIONS.view === "remote") { pressRemote(button); return; }
    switch (button) {
      case "down": moveOption(1); break;
      case "up":
        if (OPTIONS.capture) break;  // waiting for the button being recorded
        if (OPTIONS.focus === 0) closeOptions();
        else moveOption(-1);
        break;
      case "right": case "back": case "home": closeOptions(); break;
      case "ok": activateOption(); break;
      default: break;
    }
  }

  // --- recording a button ----------------------------------------------------------

  async function waitForCapture(id) {
    // While a button is being recorded the Pi doesn't pass presses on to
    // this page, so there is nothing to do but wait.
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
      await send("POST", `/api/captures/${id}/cancel`);
    } catch {
      // It finished by itself in the meantime.
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
    await refreshOptions().catch(() => {});  // stale is fine; it's read again next time
  }

  async function switchWindow(row) {
    const windowed = !row.settings.windowed;
    say(windowed ? "putting piper in a window…" : "filling the screen again…");
    try {
      await send("PUT", "/api/window", { windowed });
    } catch {
      // Usually no answer arrives: this page is the window being replaced.
    }
  }

  // --- the feed ----------------------------------------------------------------

  // "press exit again" is decided on the Pi, which also sees presses while
  // this page is hidden behind a service.
  function applyLeaving(leaving) {
    const armed = !!(leaving && leaving.armed);
    if (armed && state.notice !== "leaving") {
      notify("press exit again to close piper", true, "leaving");
    } else if (!armed && state.notice === "leaving") {
      notify("");
    }
  }

  // Everything that matters for drawing, without the timers that change on every poll.
  function signature(services) {
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
      notify("");  // it closed, or never came up
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
        // Started without desktop control: only the keyboard works.
        notify("This server is running without remote control. Use a keyboard.", true);
        state.primed = false;
        delay = 5000;
      } else if (!response.ok) {
        state.primed = false;
        delay = 2000;
      } else {
        const feed = await response.json();
        // After a reload or a server restart, the first poll only catches up;
        // old presses (an OK especially) must not be replayed.
        const continuous = state.primed && state.stream === feed.stream_id && !feed.missed;
        if (feed.missed) notify("Some presses were missed.");
        state.seen = feed.sequence;
        state.stream = feed.stream_id;
        state.session = feed.session_id || null;
        applyServices(feed.services);
        applyLeaving(feed.leaving);
        state.primed = (state.screen === "home" || state.screen === "options") && !document.hidden;
        state.control = feed.control;
        const source = feed.control === "on"
          ? feed.mode === "piper" ? "remote connected" : "desktop control selected"
          : "remote control off";
        $("home-source").textContent = source;
        $("options-state").textContent = source;
        if (continuous && state.primed && feed.control === "on" && feed.mode === "piper") {
          for (const event of feed.events || []) {
            if (event.mode === "piper" && event.session_id === feed.session_id && event.navigation) {
              // The role the key performs, which differs from the key once it is rebound.
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

  // --- start -------------------------------------------------------------------

  document.addEventListener("keydown", (event) => {
    const button = KEYS[event.key];
    if (!button) return;
    event.preventDefault();
    press(button);
  });

  window.addEventListener("resize", () => { if (state.screen === "home") layoutRing(); });

  document.addEventListener("mousemove", () => {
    document.body.classList.add("is-pointing");
    clearTimeout(pointingTimer);
    pointingTimer = setTimeout(
      () => document.body.classList.remove("is-pointing"), POINTER_IDLE_MS);
  });

  document.addEventListener("visibilitychange", () => { state.primed = false; });

  function initialize() {
    const params = new URLSearchParams(window.location.search);
    // Reopened after a service closed: start on that service's tile.
    const returning = SERVICES.findIndex((service) => service.id === params.get("focus"));
    if (returning >= 0) state.focus = returning;
    renderHistory();
    buildRing();
    renderFocus();
    $("options-corner").addEventListener("click", openOptions);
    $("options-back").addEventListener("click", closeOptions);
    $("home-exit").addEventListener("click", leavePiper);
    $("options-list").addEventListener("wheel", wheelOptions, { passive: false });
    updateClock();
    setInterval(updateClock, 20000);

    // ?boot=0 skips the splash (the Pi opens the page that way).
    $("boot-fill").style.width = "62%";
    $("boot-status").textContent = "starting · reading the remote";
    if (params.get("boot") === "0") {
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
