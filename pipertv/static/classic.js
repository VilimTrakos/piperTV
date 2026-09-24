/* The Piper interface for a television's own browser.
 *
 * Written for QtWebKit 4.8, which is what the set this was built against has:
 * ES5 and nothing newer -- no let, no arrow functions, no template strings, no
 * Promise, no fetch. What it does have is XMLHttpRequest, JSON, classList and
 * localStorage, which is enough for all of this.
 *
 * The television drives it. Its own remote reaches the browser as arrow keys,
 * and the four colour keys arrive as the codes HbbTV gave them, so no infrared
 * is involved at all -- and if Piper is serving its feed as well, the learned
 * remote drives the same dial through it.
 *
 * Opening a service replaces this page rather than opening a window: a tab is
 * something nobody can close with a remote in their hand, while the set's own
 * back button is right there and already does it.
 */
(function () {
  "use strict";

  var SERVICES = [
    { id: "search", name: "Search", letter: "⌕", colour: "#1B1F27", empty: true },
    { id: "netflix", name: "Netflix", letter: "N", colour: "#A8382F" },
    { id: "youtube", name: "YouTube", letter: "Y", colour: "#C4552F" },
    { id: "prime", name: "Prime Video", letter: "P", colour: "#1E8496" },
    { id: "disney", name: "Disney+", letter: "D", colour: "#2F4A9C" },
    { id: "hbo", name: "HBO Max", letter: "H", colour: "#6B3FA0" },
    { id: "voyo", name: "Voyo", letter: "V", colour: "#A8385A" },
    { id: "kodi", name: "Kodi", letter: "K", colour: "#3B7A57" },
    { id: "browser", name: "Web browser", letter: "L", colour: "#5A6570" }
  ];

  /* Arrows are the same everywhere. The four hundreds are the colour keys,
     which every television agrees on because HbbTV numbered them; they matter
     because OK and back are not always given to the page at all. The rest are
     what particular sets send for back. */
  var KEYS = {
    37: "left", 38: "up", 39: "right", 40: "down",
    13: "ok", 32: "ok", 415: "ok",
    8: "back", 27: "back", 461: "back", 10009: "back", 166: "back",
    403: "red", 404: "green", 405: "yellow", 406: "blue",
    36: "home", 36000: "home"
  };

  var LAST = "piper.classic.last";

  var state = { focus: 2, urls: {}, seen: 0, primed: false, feed: true, opening: false,
                session: null, launches: false, running: null, television: null };
  var tiles = [];
  var geometry = {};
  var noticeTimer = null;

  function $(id) { return document.getElementById(id); }

  function text(element, value) {
    while (element.firstChild) element.removeChild(element.firstChild);
    element.appendChild(document.createTextNode(value));
  }

  function px(value) { return Math.round(value) + "px"; }

  function place(element, left, top, width, height) {
    element.style.left = px(left);
    element.style.top = px(top);
    if (width !== undefined) element.style.width = px(width);
    if (height !== undefined) element.style.height = px(height);
  }

  // --- the shape of the screen ------------------------------------------

  function windowSize() {
    return {
      width: window.innerWidth || document.documentElement.clientWidth || 1024,
      height: window.innerHeight || document.documentElement.clientHeight || 448
    };
  }

  function measure() {
    var size = windowSize();
    var width = size.width;
    var height = size.height;
    var tile = Math.min(width * 0.075, height * 0.17);
    geometry = {
      width: width, height: height,
      pad: Math.round(width * 0.03),
      centreX: width / 2,
      centreY: height * 0.53,
      radiusX: width * 0.33,
      radiusY: height * 0.26,
      tile: tile,
      focused: tile * 1.7
    };
    return geometry;
  }

  function layout() {
    var g = measure();
    var wordmark = $("wordmark");
    wordmark.style.fontSize = px(Math.max(18, g.height * 0.085));
    place(wordmark, g.pad, g.height * 0.045);

    var clock = $("clock");
    clock.style.fontSize = px(Math.max(13, g.height * 0.05));
    place(clock, g.width - g.pad - 260, g.height * 0.055, 260);

    var source = $("source");
    source.style.fontSize = px(Math.max(11, g.height * 0.035));
    place(source, g.pad, g.height * 0.155, g.width * 0.5);

    var last = $("last");
    last.style.fontSize = px(Math.max(11, g.height * 0.035));
    last.style.textAlign = "left";
    place(last, g.pad, g.height * 0.205, g.width * 0.5);

    var name = $("name");
    name.style.fontSize = px(Math.max(16, g.height * 0.065));
    place(name, 0, g.centreY + g.radiusY + g.height * 0.03, g.width);

    var note = $("note");
    note.style.fontSize = px(Math.max(11, g.height * 0.038));
    place(note, 0, g.centreY + g.radiusY + g.height * 0.115, g.width);

    var hint = $("hint");
    hint.style.fontSize = px(Math.max(11, g.height * 0.036));
    place(hint, 0, g.height - Math.max(18, g.height * 0.065), g.width);

    var notice = $("notice");
    notice.style.fontSize = px(Math.max(12, g.height * 0.042));
    notice.style.padding = px(g.height * 0.02) + " " + px(g.width * 0.02);
    place(notice, g.width * 0.15, g.height - Math.max(60, g.height * 0.185), g.width * 0.7);

    ring();
  }

  function ring() {
    var g = geometry;
    var count = tiles.length;
    for (var index = 0; index < count; index++) {
      var tile = tiles[index];
      var focused = index === state.focus;
      var size = focused ? g.focused : g.tile;
      // Twelve o'clock is the slot above the middle, and the rest run
      // clockwise; the chosen one leaves the ring and sits in its centre.
      var offset = index - state.focus;
      var angle = (offset / count) * Math.PI * 2 - Math.PI / 2;
      var x = g.centreX + (focused ? 0 : Math.cos(angle) * g.radiusX);
      var y = g.centreY + (focused ? 0 : Math.sin(angle) * g.radiusY);
      place(tile, x - size / 2, y - size / 2, size, size);
      tile.style.lineHeight = px(size);
      tile.style.fontSize = px(size * 0.42);
      tile.className = "tile" + (focused ? " tile-focused" : "")
        + (SERVICES[index].empty ? " tile-empty" : "");
    }
  }

  function build() {
    var container = $("ring");
    for (var index = 0; index < SERVICES.length; index++) {
      var service = SERVICES[index];
      var tile = document.createElement("div");
      tile.className = "tile";
      tile.style.background = service.colour;
      tile.appendChild(document.createTextNode(service.letter));
      tile.title = service.name;
      // A click chooses, and a click on the chosen one opens it: this browser
      // has a mouse of its own, driven by the same remote.
      tile.onclick = (function (at) {
        return function () {
          if (at !== state.focus) { state.focus = at; ring(); describe(); return; }
          act("ok");
        };
      })(index);
      container.appendChild(tile);
      tiles.push(tile);
    }
  }

  // --- what it says ------------------------------------------------------

  function describe() {
    var service = SERVICES[state.focus];
    text($("name"), service.name);
    var note;
    if (state.running) note = state.running.name + " is open on the pi";
    else if (service.id === "search") note = "search is not implemented yet";
    else if (state.urls[service.id]) note = "OK or the green key opens it here";
    else if (state.launches) note = "OK or the green key opens it on the pi";
    else if (service.id === "kodi") note = "kodi plays on the pi itself, not in this browser";
    else note = "piper cannot open " + service.name + " here";
    text($("note"), note);
    text($("hint"), state.running
      ? "back or the red key closes what is open"
      : "◀ ▶ choose · OK or green opens · "
        + (state.focus + 1) + " of " + SERVICES.length);
  }

  function notify(message, keep) {
    var notice = $("notice");
    if (noticeTimer) { window.clearTimeout(noticeTimer); noticeTimer = null; }
    if (!message) { notice.className = ""; return; }
    text(notice, message);
    notice.className = "shown";
    if (!keep) noticeTimer = window.setTimeout(function () { notice.className = ""; }, 5000);
  }

  function clock() {
    var now = new Date();
    var hours = now.getHours() < 10 ? "0" + now.getHours() : "" + now.getHours();
    var minutes = now.getMinutes() < 10 ? "0" + now.getMinutes() : "" + now.getMinutes();
    text($("clock"), hours + ":" + minutes);
  }

  function ago(at) {
    var minutes = Math.round((new Date().getTime() - at) / 60000);
    if (minutes < 2) return "just now";
    if (minutes < 60) return minutes + " min ago";
    var hours = Math.round(minutes / 60);
    if (hours < 24) return hours + "h ago";
    return Math.round(hours / 24) + " days ago";
  }

  function remember(service) {
    try {
      window.localStorage.setItem(LAST, JSON.stringify(
        { id: service.id, name: service.name, at: new Date().getTime() }));
    } catch (error) { /* a set with no storage simply forgets */ }
  }

  function showLast() {
    var entry = null;
    try { entry = JSON.parse(window.localStorage.getItem(LAST) || "null"); }
    catch (error) { entry = null; }
    text($("last"), entry && entry.name ? "last opened: " + entry.name + " · " + ago(entry.at)
                                        : "nothing opened yet");
  }

  // --- moving and opening -------------------------------------------------

  function move(step) {
    var count = SERVICES.length;
    state.focus = (state.focus + step + count) % count;
    ring();
    describe();
  }

  function open() {
    var service = SERVICES[state.focus];
    if (service.id === "search") { notify("Search is not implemented yet."); return; }
    if (state.opening) return;
    var url = state.urls[service.id];
    if (url) {
      // Served to this browser: the page itself goes there. Only a browser of
      // this decade is offered a url in the first place.
      state.opening = true;
      remember(service);
      notify("Opening " + service.name + " · the TV's back button returns to piper", true);
      window.location.href = url;
      return;
    }
    if (!state.launches) {
      notify("Piper cannot open " + service.name + " from here.");
      return;
    }
    // The pi opens it on its own screen, because a television's browser can
    // show neither a modern streaming site nor the video it is protected with.
    state.opening = true;
    remember(service);
    notify("Opening " + service.name + " on the pi…", true);
    post("/api/tv/launch", { service: service.id, session_id: state.session },
      function (answer) {
        state.opening = false;
        opened(service, answer);
      },
      function (message) {
        state.opening = false;
        notify(message || ("Could not open " + service.name + "."));
      });
  }

  function opened(service, answer) {
    var television = answer && answer.television;
    if (television && television.sent) {
      notify(service.name + " is open on the pi · the TV was asked to switch to it", true);
    } else {
      notify(service.name + " is open on the pi · switch the TV to that input", true);
    }
  }

  function closeService() {
    if (!state.running) return;
    notify("Closing " + state.running.name + "…", true);
    post("/api/tv/close", {}, function () { notify(""); },
         function (message) { notify(message || "Could not close it."); });
  }

  function act(action) {
    if (state.running) {
      // Something the pi opened owns the screen. The dial must not turn behind
      // it, and the only presses that mean anything here are the way out.
      if (action === "back" || action === "red" || action === "home") closeService();
      return;
    }
    switch (action) {
      case "left": case "up": move(-1); break;
      case "right": case "down": move(1); break;
      case "ok": case "green": open(); break;
      case "home": case "red": state.focus = 0; ring(); describe(); break;
      case "back": notify("Nothing to go back to: this is piper itself."); break;
      default: break;
    }
  }

  function key(event) {
    event = event || window.event;
    var action = KEYS[event.which || event.keyCode];
    if (!action) return true;
    act(action);
    if (event.preventDefault) event.preventDefault();
    event.returnValue = false;
    return false;
  }

  // --- the pi's own feed, when there is one -------------------------------

  function post(path, payload, ok, fail) {
    var request = new XMLHttpRequest();
    request.open("POST", path, true);
    request.setRequestHeader("Content-Type", "application/json");
    request.onreadystatechange = function () {
      if (request.readyState !== 4) return;
      var answer = null;
      try { answer = JSON.parse(request.responseText); } catch (error) { answer = null; }
      if (request.status >= 200 && request.status < 300) { ok(answer || {}); return; }
      fail(answer && answer.error ? answer.error : "The pi answered " + request.status + ".");
    };
    try { request.send(JSON.stringify(payload || {})); } catch (error) { fail("The pi did not answer."); }
  }

  function ask(path, ok, fail) {
    var request = new XMLHttpRequest();
    request.open("GET", path, true);
    request.onreadystatechange = function () {
      if (request.readyState !== 4) return;
      if (request.status >= 200 && request.status < 300) {
        var answer = null;
        try { answer = JSON.parse(request.responseText); } catch (error) { answer = null; }
        if (answer) { ok(answer); return; }
      }
      fail(request.status);
    };
    try { request.send(null); } catch (error) { fail(0); }
  }

  function sourceLine(feed) {
    if (feed.served) return "served by the pi · tv remote or learned remote";
    if (state.launches) return "the dial is here · services open on the pi";
    return "the pi answers, but opens nothing from here";
  }

  function catalogue(services) {
    if (!services || !services.services) return;
    for (var index = 0; index < services.services.length; index++) {
      var service = services.services[index];
      if (service.url) state.urls[service.id] = service.url;
    }
  }

  function poll() {
    ask("/api/tv/events?after=" + state.seen, function (feed) {
      catalogue(feed.services);
      var continuous = state.primed;
      state.seen = feed.sequence || 0;
      state.primed = true;
      state.session = feed.session_id || null;
      state.television = feed.television || null;
      // A feed with no addresses in it is a pi that opens services itself: the
      // page asks it to, rather than going anywhere.
      state.launches = !feed.served
        && !!(feed.services && feed.services.available === true && feed.session_id);
      var running = (feed.services && feed.services.running) || null;
      if (running && (!state.running || state.running.id !== running.id)) {
        notify(running.name + " is open on the pi · back closes it", true);
      } else if (!running && state.running) {
        notify("");
      }
      state.running = running;
      text($("source"), sourceLine(feed));
      if (continuous && feed.control === "on" && feed.events) {
        for (var index = 0; index < feed.events.length; index++) {
          var event = feed.events[index];
          if (event.navigation) act(event.action || event.button);
        }
      }
      describe();
      window.setTimeout(poll, 500);
    }, function () {
      // No feed here: the television's own remote is the whole of it, which is
      // enough. Said once, then left alone.
      if (state.feed) {
        state.feed = false;
        text($("source"), "the tv's own remote drives this page");
      }
      window.setTimeout(poll, 10000);
    });
  }

  function start() {
    build();
    layout();
    describe();
    showLast();
    clock();
    window.setInterval(clock, 20000);
    window.setInterval(showLast, 60000);
    if (document.addEventListener) document.addEventListener("keydown", key, true);
    else document.onkeydown = key;
    if (window.addEventListener) window.addEventListener("resize", layout, false);
    else window.onresize = layout;
    // The set's own browser can hide its toolbars, which gives the page most
    // of the screen back. Whether it reports that as a resize is its business;
    // the size is watched instead, which is true of any engine.
    window.setInterval(function () {
      var size = windowSize();
      if (size.width !== geometry.width || size.height !== geometry.height) layout();
    }, 1000);
    poll();
  }

  start();
})();
