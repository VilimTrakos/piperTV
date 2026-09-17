"use strict";

/* The keyboard Piper puts over a page whose search box was selected.
 *
 * It is a page like the interface itself, and it is driven the same way: by
 * numbered presses from /api/tv/events rather than by keystrokes, so the four
 * arrows and OK are enough and a keyboard is never needed to work a keyboard.
 *
 * Nothing is typed into the page underneath as it goes. What is composed here
 * is handed to the Pi on OK, which closes this window and types it into the
 * field that asked for it -- one string, once, into a window that by then has
 * its focus back.
 */

(() => {
  const $ = (id) => document.getElementById(id);

  const ROWS = [
    "1234567890".split(""),
    "qwertyuiop".split(""),
    "asdfghjkl".split("").concat(["-"]),
    "zxcvbnm".split("").concat([".", "/", "@"]),
    [{ id: "space", label: "space", wide: true },
     { id: "backspace", label: "delete", wide: true },
     { id: "clear", label: "clear", wide: true },
     { id: "cancel", label: "cancel", wide: true },
     { id: "search", label: "search", wide: true }],
  ];

  const state = { text: "", row: 1, column: 0, seen: 0, stream: null,
    primed: false, keys: [], sending: false };

  function build() {
    const grid = $("keys-grid");
    grid.replaceChildren();
    state.keys = ROWS.map((row) => row.map((entry) => {
      const key = typeof entry === "string"
        ? { id: entry, label: entry, wide: false } : entry;
      const element = document.createElement("div");
      element.className = `key${key.wide ? " is-wide is-word" : ""}`;
      element.textContent = key.label;
      grid.append(element);
      return { ...key, element };
    }));
    render();
  }

  function render() {
    state.keys.forEach((row, rowIndex) => row.forEach((key, columnIndex) => {
      key.element.classList.toggle("is-on",
        rowIndex === state.row && columnIndex === state.column);
    }));
    $("typed-text").textContent = state.text;
    $("keys-hint").textContent =
      "◀ ▲ ▼ ▶ choose · OK types · back cancels";
  }

  function move(rowStep, columnStep) {
    const rows = state.keys.length;
    if (rowStep) {
      state.row = (state.row + rowStep + rows) % rows;
      // Keep roughly the same place across rows of different lengths.
      state.column = Math.min(state.column, state.keys[state.row].length - 1);
    }
    if (columnStep) {
      const columns = state.keys[state.row].length;
      state.column = (state.column + columnStep + columns) % columns;
    }
    render();
  }

  async function finish(text) {
    if (state.sending) return;
    state.sending = true;
    try {
      await fetch("/api/tv/type", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ text }),
      });
    } catch {
      state.sending = false;   // the Pi will close this window when it can
    }
  }

  function press(button) {
    switch (button) {
      case "up": move(-1, 0); break;
      case "down": move(1, 0); break;
      case "left": move(0, -1); break;
      case "right": move(0, 1); break;
      case "back": case "exit": finish(null); break;
      case "ok": {
        const key = state.keys[state.row][state.column];
        if (key.id === "search") finish(state.text);
        else if (key.id === "cancel") finish(null);
        else if (key.id === "clear") state.text = "";
        else if (key.id === "backspace") state.text = state.text.slice(0, -1);
        else if (key.id === "space") state.text += " ";
        else state.text += key.id;
        render();
        break;
      }
      default: break;
    }
  }

  const KEYBOARD = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left",
    ArrowRight: "right", Enter: "ok", Escape: "back", Backspace: "back" };

  document.addEventListener("keydown", (event) => {
    const button = KEYBOARD[event.key];
    if (!button) return;
    event.preventDefault();
    press(button);
  });

  async function poll() {
    let delay = 200;
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 3000);
    try {
      const response = await fetch(`/api/tv/events?after=${state.seen}`, {
        headers: { Accept: "application/json" }, signal: abort.signal,
      });
      if (!response.ok) {
        state.primed = false;
        delay = 2000;
      } else {
        const feed = await response.json();
        const continuous = state.primed && state.stream === feed.stream_id && !feed.missed;
        state.seen = feed.sequence;
        state.stream = feed.stream_id;
        state.primed = !document.hidden;
        $("keys-target").textContent = (feed.typing && feed.typing.label)
          ? `typing into ${feed.typing.label}` : "typing";
        if (continuous && feed.control === "on") {
          for (const event of feed.events || []) {
            if (event.navigation) press(event.action || event.button);
          }
        }
      }
    } catch {
      state.primed = false;
      delay = 2000;
    } finally {
      clearTimeout(timeout);
    }
    setTimeout(poll, delay);
  }

  document.addEventListener("visibilitychange", () => { state.primed = false; });

  build();
  poll();
})();
