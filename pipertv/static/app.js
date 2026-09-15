"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const svg = (body) => `<svg viewBox="0 0 24 24" aria-hidden="true">${body}</svg>`;
  const icons = {
    power: svg('<path d="M12 3v9M7 5.5a8 8 0 1 0 10 0"/>'),
    home: svg('<path d="m3 10 9-7 9 7M6 8v12h5v-7h3v7h4V8"/>'),
    back: svg('<path d="m9 5-5 5 5 5M5 10h10a5 5 0 0 1 0 10h-4"/>'),
    guide: svg('<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 9v6m4-6v6m4-6v6"/>'),
    menu: svg('<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 9h8M8 12h8M8 15h5"/>'),
    list: svg('<path d="M5 5h14v14H5zM8 9h8M8 12h8M8 15h5"/>'),
    source: svg('<rect x="6" y="5" width="15" height="14" rx="2"/><path d="M2 12h12m-4-4 4 4-4 4"/>'),
    mute: svg('<path d="M4 9h4l5-4v14l-5-4H4zM17 9l5 6m0-6-5 6"/>'),
    volume_up: svg('<path d="M4 17 20 7v10z"/>'),
    volume_down: svg('<path d="m7 16 12-7v7z"/>'),
    channel_up: svg('<path d="M12 5v14M5 12h14"/>'),
    channel_down: svg('<path d="M5 12h14"/>'),
    up: svg('<path d="m12 6 6 10H6z"/>'),
    down: svg('<path d="m12 18 6-10H6z"/>'),
    left: svg('<path d="m6 12 10-6v12z"/>'),
    right: svg('<path d="m18 12-10-6v12z"/>'),
    rewind: svg('<path d="m11 5-8 7 8 7zm10 0-8 7 8 7z"/>'),
    fast_forward: svg('<path d="m3 5 8 7-8 7zm10 0 8 7-8 7z"/>'),
    play: svg('<path d="m7 4 12 8-12 8z"/>'),
    pause: svg('<path d="M8 5v14M16 5v14" stroke-width="4"/>'),
    previous: svg('<path d="M5 5v14m14-14L8 12l11 7z"/>'),
    next: svg('<path d="M19 5v14M5 5l11 7-11 7z"/>'),
    stop: svg('<rect x="6" y="6" width="12" height="12" fill="currentColor" stroke="none"/>'),
    record: svg('<circle cx="12" cy="12" r="6" fill="currentColor" stroke="none"/>'),
    audio: svg('<path d="M6 18V6l12-2v12M6 18c0 4-6 4-6 0s6-4 6 0zm12-2c0 4-6 4-6 0s6-4 6 0"/>'),
    format: svg('<rect x="2" y="5" width="20" height="14" rx="1"/><path d="m6 9 3 3-3 3m12-6-3 3 3 3"/>'),
  };
  const shortLabels = {
    favorites: "fav", help: "?", info: "i", exit: "EXIT", ok: "OK", media: "MEDIA",
    nettv: "NETTV", three_d: "3D", tv_radio: "TV/RAD", red: "Red", green: "Green", yellow: "Yellow", blue: "Blue",
  };
  let state = null;
  let selectedId = "power";
  let selectedSample = 0;
  let health = { ok: false };
  let activeCapture = null;
  let busy = false;
  let mutating = false;
  let pollTimer = null;
  let lastStatus = "idle";
  let control = null;
  let controlTimer = null;
  let controlActionError = "";

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, { ...options, signal: controller.signal,
        headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
      const text = await response.text();
      let data;
      try { data = text ? JSON.parse(text) : {}; } catch { throw new Error(`The app returned an unexpected response (${response.status}).`); }
      if (!response.ok) {
        const error = new Error(data.error || data.message || `Request failed (${response.status}).`);
        error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The app did not respond in time. Check that the Python app is still running.");
      if (error instanceof TypeError) throw new Error("Cannot reach the Python app. Check that it is still running, then reload this page.");
      throw error;
    } finally { clearTimeout(timer); }
  }

  function showError(message) {
    $("global-error").textContent = message || "";
    $("global-error").hidden = !message;
  }

  function buttonMetadata() { return state?.buttons.find((button) => button.id === selectedId); }
  function samples() { return state?.recordings?.[selectedId]?.samples || []; }
  function keyContent(id) {
    if (icons[id]) return icons[id];
    if (id.startsWith("digit_")) return id.slice(6);
    return shortLabels[id] || "·";
  }

  function makeKey(id, classes = "") {
    const metadata = state.buttons.find((button) => button.id === id);
    if (!metadata) return null;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `remote-key ${classes}`;
    button.dataset.id = id;
    button.innerHTML = keyContent(id);
    button.title = metadata.label;
    button.setAttribute("aria-label", metadata.label);
    button.addEventListener("click", () => selectButton(id, true));
    return button;
  }

  function makeRow(ids, classes = "", keyClasses = "") {
    const row = document.createElement("div");
    row.className = `key-row ${classes}`;
    ids.forEach((id) => { const key = makeKey(id, `${keyClasses} ${id}`); if (key) row.append(key); });
    return row;
  }

  function buildRemote() {
    const keys = $("remote-keys");
    keys.replaceChildren();
    keys.append(makeRow(["power"], "power-row", "round power-key"));
    [[1, 2, 3], [4, 5, 6], [7, 8, 9]].forEach((row) => keys.append(makeRow(row.map((digit) => `digit_${digit}`), "number-row", "round")));
    keys.append(makeRow(["favorites", "digit_0", "guide"], "number-row", "round"));
    keys.querySelector('[data-id="favorites"]')?.classList.add("tiny-label");
    keys.append(makeRow(["red", "green", "yellow", "blue"], "color-row"));
    keys.append(makeRow(["exit", "help", "info"], "small-row"));
    const navigation = document.createElement("div");
    navigation.className = "nav-panel";
    navigation.append(makeRow(["home", "back"], "nav-corners"));
    const pad = document.createElement("div");
    pad.className = "nav-pad";
    ["up", "left", "ok", "right", "down"].forEach((id) => { const key = makeKey(id); if (key) pad.append(key); });
    navigation.append(pad, makeRow(["menu", "list"], "nav-corners"));
    keys.append(navigation);
    keys.append(makeRow(["volume_up", "source", "channel_up"], "volume-row"));
    keys.append(makeRow(["volume_down", "mute", "channel_down"], "volume-row"));
    keys.append(makeRow(["media", "nettv"], "media-row"));
    [["rewind", "play", "fast_forward"], ["previous", "pause", "next"], ["record", "three_d", "stop"], ["tv_radio", "audio", "format"]]
      .forEach((ids) => keys.append(makeRow(ids, "transport-row")));
  }

  function syncKeyStates() {
    document.querySelectorAll(".remote-key").forEach((key) => {
      const id = key.dataset.id;
      const metadata = state.buttons.find((button) => button.id === id);
      const count = state.recordings?.[id]?.samples?.length || 0;
      key.classList.toggle("is-selected", id === selectedId);
      key.classList.toggle("is-saved", count > 0);
      key.setAttribute("aria-pressed", String(id === selectedId));
      key.title = `${metadata.label}${count ? ` · ${count} saved` : ""}`;
      key.setAttribute("aria-label", `${metadata.label}${count ? `, ${count} saved recording${count === 1 ? "" : "s"}` : ""}`);
      key.disabled = busy || mutating;
    });
    $("remote").classList.toggle("remote-busy", busy);
  }

  function updateControls() {
    if (!state) return;
    syncKeyStates();
    $("record-button").disabled = busy || mutating || (state.mode !== "demo" && !health.ok);
    $("record-label").textContent = busy ? "Recording…" : samples().length ? "Record another signal" : "Record signal";
    $("cancel-capture").hidden = !busy;
    $("cancel-capture").disabled = !activeCapture;
    ["rename-button", "sample-select", "delete-sample", "test-receiver"]
      .forEach((id) => { $(id).disabled = busy || mutating; });
    $("rename-form").querySelectorAll("input,button").forEach((element) => { element.disabled = busy || mutating; });
  }

  function renderState() {
    $("demo-banner").hidden = state.mode !== "demo";
    $("total-count").textContent = state.buttons.length;
    $("saved-count").textContent = state.buttons.filter((button) => state.recordings?.[button.id]?.samples?.length).length;
    $("storage-path").textContent = state.data_file || "Local JSON library";
    $("storage-path").title = state.data_file || "Local JSON library";
    const metadata = buttonMetadata();
    $("selected-label").textContent = metadata.label;
    $("selected-id").textContent = metadata.id;
    $("selected-section").textContent = (metadata.section || "remote").replaceAll("_", " ").toUpperCase();
    $("selected-icon").innerHTML = icons[selectedId] || `<span class="${selectedId.startsWith("digit_") ? "" : "tiny-icon-text"}">${keyContent(selectedId)}</span>`;
    renderSamples();
    updateControls();
  }

  function selectButton(id, scroll = false) {
    if (busy || mutating || !state.buttons.some((button) => button.id === id)) return;
    selectedId = id;
    selectedSample = 0;
    $("rename-form").hidden = true;
    setCaptureStatus("idle");
    renderState();
    if (scroll && window.matchMedia("(max-width:660px)").matches) {
      $("capture-heading").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "start" });
    }
  }

  function renderConnection() {
    const demo = state?.mode === "demo";
    const badge = $("connection-badge");
    badge.className = `status-badge ${demo ? "demo" : health.ok ? "connected" : "disconnected"}`;
    badge.querySelector("span").textContent = demo ? "Demo" : health.ok ? "Connected" : "Offline";
    $("receiver-detail").textContent = demo ? "Simulated signals · hardware not in use" : "Raspberry Pi · GPIO receiver";
    $("local-receiver-error").hidden = !!health.ok;
    $("local-receiver-error").textContent = health.error || "The receiver is unavailable. Check its wiring and the server configuration, then test again.";
    updateControls();
  }

  function setCaptureStatus(status, error = "") {
    lastStatus = status;
    const label = buttonMetadata()?.label || "the selected button";
    const demo = state?.mode === "demo";
    const statuses = {
      idle: ["↗", "Ready when you are", demo ? "Click Record to create a clearly marked example signal and try the recording workflow." : "Click Record, then point your real remote at the receiver and press the selected button once."],
      arming: ["·", "Preparing receiver…", demo ? "Preparing a simulated recording. No hardware is being used." : "Wait for the receiver to start listening before pressing your remote."],
      listening: ["◉", demo ? "Simulating a remote press…" : `Listening — press ${label}`, demo ? "Generating a sample infrared envelope for this button." : "Point the real remote at the sensor and give the button one short press. Hold the remote steady."],
      cancelling: ["·", "Cancelling…", "Waiting for the receiver to finish. You can start another recording once it is ready."],
      captured: ["✓", "Signal saved", demo ? "Your simulated signal is saved. Record another sample, or select the next button." : "Your recording is safely stored in the library. Try another sample, or choose the next button."],
      timeout: ["⌛", "No signal received", "Try again with the remote closer to the sensor. Check the receiver wiring, then wait for Listening before pressing."],
      cancelled: ["↗", "Recording cancelled", "Nothing was saved. You can record this button again whenever you’re ready."],
      error: ["!", "Couldn’t record the signal", error || "Check the receiver connection and try again."],
    };
    const [icon, title, copy] = statuses[status] || statuses.error;
    $("capture-status").className = `capture-status ${status}`;
    $("capture-status-icon").textContent = icon;
    $("capture-status-title").textContent = title;
    $("capture-status-copy").textContent = copy;
  }

  function signalDurations(signal) {
    const values = signal.durations_us || signal.pulses_us || signal.timings_us || [];
    return Array.isArray(values) ? values.map(Number).filter((value) => Number.isFinite(value) && value > 0) : [];
  }

  function readableDate(value, compact = false) {
    if (!value) return "Unknown";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Unknown";
    return date.toLocaleString(undefined, compact ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" } : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function renderWaveform(durations) {
    const container = $("waveform");
    container.replaceChildren();
    const total = durations.reduce((sum, value) => sum + value, 0);
    $("waveform-duration").textContent = total ? `${(total / 1000).toFixed(1)} ms` : "—";
    if (!total) {
      const label = document.createElement("span");
      label.className = "waveform-empty";
      label.textContent = "No pulse timings available";
      container.append(label);
      return;
    }
    const ns = "http://www.w3.org/2000/svg";
    const drawing = document.createElementNS(ns, "svg");
    drawing.setAttribute("viewBox", "0 0 600 60");
    drawing.setAttribute("preserveAspectRatio", "none");
    drawing.setAttribute("role", "img");
    drawing.setAttribute("aria-label", `${durations.length} alternating pulse and space intervals spanning ${(total / 1000).toFixed(1)} milliseconds`);
    let elapsed = 0;
    let path = "M 0 48";
    durations.forEach((duration, index) => {
      path += ` V ${index % 2 === 0 ? 12 : 48} H ${((elapsed + duration) / total * 600).toFixed(3)}`;
      elapsed += duration;
    });
    path += " V 48";
    const line = document.createElementNS(ns, "path");
    line.setAttribute("d", path);
    drawing.append(line);
    container.append(drawing);
  }

  function renderSamples() {
    const saved = samples();
    $("sample-count").textContent = saved.length;
    $("samples-empty").hidden = saved.length > 0;
    $("samples-content").hidden = saved.length === 0;
    if (!saved.length) return;
    selectedSample = Math.max(0, Math.min(selectedSample, saved.length - 1));
    const select = $("sample-select");
    select.replaceChildren();
    saved.forEach((signal, index) => {
      const option = document.createElement("option");
      option.value = index;
      option.textContent = `Sample ${index + 1} · ${readableDate(signal.captured_at || signal.timestamp)}${signal.simulated || signal.demo || signal.source === "demo" ? " · DEMO" : ""}`;
      select.append(option);
    });
    select.value = String(selectedSample);
    const signal = saved[selectedSample];
    const durations = signalDurations(signal);
    renderWaveform(durations);
    $("signal-intervals").textContent = durations.length || "—";
    const carrier = signal.carrier_hz || signal.frequency_hz || 38000;
    $("signal-carrier").textContent = `${(carrier / 1000).toLocaleString()} kHz ${signal.carrier_source === "measured" ? "measured" : "assumed"}`;
    $("signal-date").textContent = readableDate(signal.captured_at || signal.timestamp, true);
    $("signal-json").textContent = JSON.stringify(signal, null, 2);
    $("export-sample").href = `/api/export/${encodeURIComponent(selectedId)}?sample=${selectedSample}&format=irctl`;
    $("export-sample").download = `${selectedId}-${selectedSample + 1}.ir`;
  }

  async function refreshState() {
    state = await api("/api/state");
    if (!state.buttons?.length) throw new Error("No remote buttons are configured in the recording library.");
    if (!state.buttons.some((button) => button.id === selectedId)) selectedId = state.buttons[0].id;
    renderState();
  }

  async function finishCapture(job) {
    clearTimeout(pollTimer);
    activeCapture = null;
    busy = false;
    setCaptureStatus(job.status, job.error);
    if (job.status === "captured") {
      try {
        await refreshState();
        selectedSample = Math.max(0, samples().length - 1);
        renderSamples();
      } catch (error) { showError(`The signal was captured, but the library could not be refreshed: ${error.message}`); }
    }
    updateControls();
  }

  async function pollCapture() {
    if (!activeCapture) return;
    const id = activeCapture;
    try {
      const job = await api(`/api/captures/${encodeURIComponent(id)}`);
      if (activeCapture !== id) return;
      if (["captured", "timeout", "cancelled", "error"].includes(job.status)) await finishCapture(job);
      else {
        setCaptureStatus(job.status);
        pollTimer = setTimeout(pollCapture, 250);
      }
    } catch (error) {
      if (activeCapture !== id) return;
      if (error.status === 404) {
        await finishCapture({ status: "error", error: "This recording is no longer active. The server may have restarted. Check the saved signals, then record again." });
        return;
      }
      // Keep the association locked while its server-side capture may still be live.
      setCaptureStatus("error", `${error.message} Reconnecting to check this recording…`);
      pollTimer = setTimeout(pollCapture, 1500);
    }
  }

  $("record-button").addEventListener("click", async () => {
    if (busy || mutating) return;
    busy = true;
    showError("");
    $("rename-form").hidden = true;
    setCaptureStatus("arming");
    updateControls();
    try {
      const job = await api("/api/captures", { method: "POST", body: JSON.stringify({ button_id: selectedId, timeout_s: 10, gap_us: 120000, max_duration_s: 3 }) });
      if (!job.id) throw new Error("The receiver did not return a recording ID. Reconnect and try again.");
      activeCapture = job.id;
      updateControls();
      if (["captured", "timeout", "cancelled", "error"].includes(job.status)) await finishCapture(job);
      else { setCaptureStatus(job.status || "arming"); pollTimer = setTimeout(pollCapture, 100); }
    } catch (error) {
      busy = false;
      activeCapture = null;
      setCaptureStatus("error", error.message);
      updateControls();
    }
  });

  $("cancel-capture").addEventListener("click", async () => {
    if (!activeCapture) return;
    $("cancel-capture").disabled = true;
    const id = activeCapture;
    try {
      const job = await api(`/api/captures/${encodeURIComponent(id)}/cancel`, { method: "POST", body: "{}" });
      if (activeCapture !== id) return;
      if (["captured", "timeout", "cancelled", "error"].includes(job.status)) await finishCapture(job);
      else setCaptureStatus("cancelling");
    } catch (error) { showError(`Could not cancel: ${error.message} The app will keep checking the receiver.`); }
    finally { updateControls(); }
  });

  $("test-receiver").addEventListener("click", async () => {
    if (busy || mutating) return;
    mutating = true;
    $("test-receiver").textContent = "Testing receiver…";
    updateControls();
    try {
      health = await api("/api/health");
      await refreshState();
    } catch (error) { health = { ok: false, error: error.message }; }
    finally {
      mutating = false;
      $("test-receiver").textContent = "Test receiver";
      renderConnection();
    }
  });

  $("rename-button").addEventListener("click", () => {
    if (busy || mutating) return;
    $("rename-form").hidden = false;
    $("button-label").value = buttonMetadata().label;
    $("button-label").focus();
    $("button-label").select();
  });
  $("cancel-rename").addEventListener("click", () => { $("rename-form").hidden = true; $("rename-button").focus(); });
  $("rename-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const label = $("button-label").value.trim();
    if (!label || busy || mutating) return;
    mutating = true;
    updateControls();
    try {
      state = await api(`/api/buttons/${encodeURIComponent(selectedId)}`, { method: "PUT", body: JSON.stringify({ label }) });
      $("rename-form").hidden = true;
      showError("");
      renderState();
      if (lastStatus === "idle") setCaptureStatus("idle");
    } catch (error) { showError(error.message); }
    finally { mutating = false; updateControls(); }
  });
  $("sample-select").addEventListener("change", () => { selectedSample = Number($("sample-select").value); renderSamples(); });
  $("delete-sample").addEventListener("click", async () => {
    if (busy || mutating) return;
    if (!window.confirm(`Delete sample ${selectedSample + 1} for ${buttonMetadata().label}? Other recordings will be kept.`)) return;
    mutating = true;
    updateControls();
    try {
      state = await api(`/api/buttons/${encodeURIComponent(selectedId)}/samples/${selectedSample}`, { method: "DELETE" });
      showError("");
      renderState();
      setCaptureStatus("idle");
    } catch (error) { showError(error.message); }
    finally { mutating = false; updateControls(); }
  });

  function showControlError(message) {
    $("control-error").textContent = message || "";
    $("control-error").hidden = !message;
  }

  function renderControl() {
    if (!control) { $("control-card").hidden = true; return; }
    $("control-card").hidden = false;
    const on = control.control === "on";
    const badge = $("control-badge");
    badge.className = `status-badge ${on ? "connected" : "disconnected"}`;
    const manual = control.origin === "manual";
    badge.querySelector("span").textContent = on ? `${manual ? "Manual" : "On"} · ${control.mode}` : "Off";
    $("control-detail").textContent = control.hold || (manual
      ? "Manual confirmation: you said the TV is showing the Pi. Stop control when you switch away."
      : control.detail || "");
    // A mode belongs to one visit, so the choice reappears on every new one.
    $("control-modes").hidden = !control.needs_mode;
    $("control-manual").hidden = Boolean(control.session);
    $("control-stop").hidden = !control.session;
    const runtime = control.runtime || {};
    const problem = [runtime.pointer, runtime.receiver, runtime.desktop,
      ...(control.mode === "snapping" ? [runtime.targets] : [])].find(part => part?.error);
    $("control-runtime-error").textContent = problem?.error || "";
    $("control-runtime-error").hidden = !problem;
  }

  async function refreshControl() {
    try {
      control = await api("/api/control");
      showControlError(controlActionError);
    } catch (error) {
      // 404 means this server was started without desktop control.
      if (error.status === 404) { control = null; renderControl(); return; }
      showControlError(error.message);
      return;
    }
    renderControl();
  }

  function pollControl() {
    clearTimeout(controlTimer);
    // The TV can change input at any time, so the gate is re-read on a timer.
    controlTimer = setTimeout(async () => { await refreshControl(); pollControl(); }, 2500);
  }

  async function controlAction(path, payload) {
    try {
      control = await api(path, { method: "POST", body: JSON.stringify(payload) });
      controlActionError = "";
      showControlError("");
      renderControl();
    } catch (error) {
      controlActionError = error.message;
      showControlError(controlActionError);
      await refreshControl();
    }
  }

  async function chooseMode(mode) {
    if (!control?.session) return;
    await controlAction("/api/control/mode", { mode, session_id: control.session.id });
  }

  $("mode-pointer").addEventListener("click", () => chooseMode("pointer"));
  $("mode-snapping").addEventListener("click", () => chooseMode("snapping"));
  $("mode-piper").addEventListener("click", () => chooseMode("piper"));
  // --- how the cursor moves ------------------------------------------------
  // A preference rather than a mode: it outlives the visit, so it is saved
  // with the recordings and read back at startup.
  let pointer = null;

  function renderPointer() {
    if (!pointer) return;
    const settings = pointer.settings;
    $("drive-snap").className = settings.drive === "nudge" ? "button button-outline" : "button button-primary";
    $("drive-nudge").className = settings.drive === "nudge" ? "button button-primary" : "button button-outline";
    $("pointer-step").value = settings.step_px;
    $("pointer-max").value = settings.max_step_px;
    $("pointer-accelerate").value = settings.accelerate_within_s;
    $("pointer-scroll").value = settings.scroll_clicks;
  }

  async function savePointer(values, note) {
    $("pointer-status").textContent = "";
    try {
      pointer = await api("/api/pointer", { method: "PUT", body: JSON.stringify(values) });
      renderPointer();
      $("pointer-status").textContent = note || "Saved.";
    } catch (error) {
      $("pointer-status").textContent = error.message;
    }
  }

  const fields = () => ({
    drive: pointer?.settings.drive ?? "snap",
    step_px: Number($("pointer-step").value),
    max_step_px: Number($("pointer-max").value),
    accelerate_within_s: Number($("pointer-accelerate").value),
    scroll_clicks: Number($("pointer-scroll").value),
  });

  $("drive-snap").addEventListener("click", () => savePointer({ ...fields(), drive: "snap" },
    "Snapping between the controls a page reports."));
  $("drive-nudge").addEventListener("click", () => savePointer({ ...fields(), drive: "nudge" },
    "Moving the cursor itself, faster the longer a direction is held."));
  $("pointer-save").addEventListener("click", () => savePointer(fields()));
  $("pointer-reset").addEventListener("click", () => savePointer(pointer?.defaults ?? {},
    "Back to the defaults."));

  $("control-confirm").addEventListener("click", () => controlAction("/api/control/manual", { confirmed: true }));
  $("control-stop").addEventListener("click", () => controlAction("/api/control/stop", {}));

  async function initialize() {
    try {
      state = await api("/api/state");
      pointer = await api("/api/pointer").catch(() => null);
      renderPointer();
      if (!state.buttons?.length) throw new Error("No remote buttons are configured in the recording library.");
      if (!state.buttons.some((button) => button.id === selectedId)) selectedId = state.buttons[0].id;
      buildRemote();
      renderState();
      setCaptureStatus("idle");
      if (state.active_capture && ["arming", "listening", "cancelling"].includes(state.active_capture.status)) {
        selectedId = state.active_capture.button_id;
        activeCapture = state.active_capture.id;
        busy = true;
        renderState();
        setCaptureStatus(state.active_capture.status);
        pollTimer = setTimeout(pollCapture, 100);
      }
      try { health = await api("/api/health"); } catch (error) { health = { ok: false, error: error.message }; }
      renderConnection();
      await refreshControl();
      pollControl();
    } catch (error) {
      showError(error.message);
      $("receiver-detail").textContent = "App unavailable";
      $("connection-badge").querySelector("span").textContent = "Offline";
      $("connection-badge").classList.add("disconnected");
    }
  }
  initialize();
})();
