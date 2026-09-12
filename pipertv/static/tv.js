"use strict";

/* The Piper interface, driven by the learned remote.
 *
 * Presses arrive as numbered data from /api/tv/events rather than as
 * synthesised keystrokes, so this page can be developed and judged with a
 * keyboard and behaves the same either way.
 *
 * The service list and launch history below are placeholders carried over from
 * the design references. Piper does not yet know what is installed on this Pi,
 * and opening a service is not implemented, so OK says so rather than
 * pretending. Replacing these two arrays with real data is the next step.
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

  const HISTORY = [
    { name: "Netflix", letter: "N", colour: "#A8382F", when: "40 min ago · 1h 12m" },
    { name: "YouTube", letter: "Y", colour: "#C4552F", when: "yesterday · 26m" },
    { name: "Plex", letter: "J", colour: "#C39A22", when: "2 days ago · 2h 04m" },
    { name: "Kodi", letter: "K", colour: "#3B7A57", when: "4 days ago · 48m" },
    { name: "Prime Video", letter: "P", colour: "#1E8496", when: "last week · 1h 36m" },
  ];

  // Design coordinates; the stylesheet scales them through --u.
  const CENTRE = { x: 960, y: 560 };
  const RADIUS = 300;
  const TILE = 120;
  const FOCUSED_TILE = 200;

  const KEYS = {
    ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
    Enter: "ok", Backspace: "back", Escape: "back", h: "home", m: "menu",
  };

  const state = { screen: "boot", focus: 1, seen: 0, control: "off", tiles: [] };
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

  function renderHistory() {
    const list = $("history-list");
    list.replaceChildren();
    for (const entry of HISTORY) {
      const item = document.createElement("li");
      item.className = "history-item";
      const badge = document.createElement("span");
      badge.className = "history-badge";
      badge.textContent = entry.letter;
      badge.style.background = entry.colour;
      const text = document.createElement("div");
      const name = document.createElement("div");
      name.className = "history-name";
      name.textContent = entry.name;
      const when = document.createElement("div");
      when.className = "history-when";
      when.textContent = entry.when;
      text.append(name, when);
      item.append(badge, text);
      list.append(item);
    }
  }

  function renderFocus() {
    const service = SERVICES[state.focus];
    $("focus-name").textContent = service.name;
    $("focus-note").textContent = service.kind === "search"
      ? "OK to search" : "OK to open";
    $("ring-hint").textContent =
      `◀ ▶ choose · OK opens · ${state.focus + 1} of ${SERVICES.length}`;
  }

  function showScreen(name) {
    state.screen = name;
    $("screen-boot").classList.toggle("is-shown", name === "boot");
    $("screen-home").classList.toggle("is-shown", name === "home");
  }

  function notify(message, persist = false) {
    clearTimeout(noticeTimer);
    const notice = $("tv-notice");
    notice.textContent = message || "";
    notice.hidden = !message;
    if (message && !persist) noticeTimer = setTimeout(() => { notice.hidden = true; }, 4000);
  }

  function clock() {
    const now = new Date();
    const time = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
    $("home-clock").textContent = time;
    $("boot-meta").textContent = `raspberry pi · ${time}`;
  }

  function move(step) {
    const count = SERVICES.length;
    state.focus = (state.focus + step + count) % count;
    layoutRing();
    renderFocus();
  }

  function press(button) {
    if (state.screen !== "home") return;
    switch (button) {
      case "right": move(1); break;
      case "left": move(-1); break;
      case "down": move(1); break;
      case "up": move(-1); break;
      case "ok": {
        const service = SERVICES[state.focus];
        // Opening a service is not implemented. The design references mock it;
        // saying so is better than a screen that pretends something launched.
        notify(`${service.name} — opening services is not implemented yet.`);
        break;
      }
      case "home": move(-state.focus); break;
      default: break;
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
    try {
      const response = await fetch(`/api/tv/events?after=${state.seen}`, {
        headers: { Accept: "application/json" },
      });
      if (response.status === 404) {
        // Started without desktop control: the page still works by keyboard.
        notify("This server is running without remote control. Use a keyboard.", true);
        delay = 5000;
      } else if (!response.ok) {
        delay = 2000;
      } else {
        const feed = await response.json();
        if (feed.missed) notify("Some presses were missed.");
        state.seen = feed.sequence;
        state.control = feed.control;
        $("home-source").textContent = feed.control === "on"
          ? "remote connected" : "waiting for the TV";
        for (const event of feed.events || []) press(event.button);
      }
    } catch {
      delay = 3000;
    }
    setTimeout(poll, delay);
  }

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
