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
    { id: "plex", name: "Plex", letter: "J", colour: "#C39A22" },
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

  function press(button) {
    if (state.screen !== "home") return;
    if (state.open) {
      // Something owns the screen; the ring must not move behind it, and back
      // belongs to that service. Only exit and home come back here.
      if (button === "exit" || button === "home") closeService();
      return;
    }
    switch (button) {
      case "right": move(1); break;
      case "left": move(-1); break;
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
        state.primed = state.screen === "home" && !document.hidden;
        state.control = feed.control;
        $("home-source").textContent = feed.control === "on"
          ? feed.mode === "piper" ? "remote connected" : "desktop control selected"
          : "remote control off";
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
    renderHistory();
    buildRing();
    renderFocus();
    clock();
    setInterval(clock, 20000);

    // A kiosk that reloads should not replay the splash every time, and it is
    // the only way to look at the dial in a renderer that cannot wait.
    const skipBoot = new URLSearchParams(window.location.search).get("boot") === "0";
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
