"""The Flask app: the recording studio at /, the TV interface at /tv, and the API."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import signal
import socket
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

from .config import CONFIG, read_pin, write_pin
from .control import RemoteControl
from .desktop import DRIVES, POINTER_DEFAULTS, POINTER_LIMITS, validate_pointer
from .gpio_ir import (AUTO, CONSUMER, DEFAULT_PIN, KERNEL_RECEIVER, LIRC, describe,
                      kernel_line, list_lines, resolve, validate_pin)
from .recorder import Recorder
from .roles import SUGGESTED, RoleMap
from .storage import RecordingStore
from .window import WINDOW_DEFAULTS, WINDOW_LIMITS, validate_window

STATIC = Path(__file__).parent / "static"
LOG = logging.getLogger("pipertv.app")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")


def gpio_lines():
    try:
        return list_lines(), None
    except OSError as exc:
        return [], f"This machine's GPIO pins cannot be read: {exc}"


def create_app(data: str | Path | None = None, device: str = LIRC, demo: bool = False,
               recorder: Recorder | None = None, control: bool = False,
               remote: RemoteControl | None = None, browser: str | None = None,
               port: int = 8765, config: str | Path | None = None) -> Flask:
    """Build the app. Letting the remote drive the desktop (control) is opt-in."""
    config_path = Path(config) if config else CONFIG
    pin = read_pin(config_path)
    if recorder is None:
        store = RecordingStore(data or Path("data/demo-recordings.json" if demo
                                            else "data/recordings.json"))
        receiver = resolve(pin, gpio_lines()[0], lirc=device)
        if remote is None and control and not demo:
            remote = RemoteControl(store, device=receiver, browser=browser, port=port)
        recorder = Recorder(store, device=receiver, demo=demo, gate=remote)
    store = recorder.store

    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 32768
    app.json.sort_keys = False
    app.extensions["recorder"] = recorder
    app.extensions["remote"] = remote
    if remote is not None:
        remote.start()

    local_names = {"localhost", socket.gethostname().lower(), socket.getfqdn().lower()}
    local_names |= {name + ".local" for name in tuple(local_names) if "." not in name}

    @app.before_request
    def check_request():
        # Only the Pi's own name or a LAN address, so DNS rebinding can't reach the API.
        host = (urlsplit(request.host_url).hostname or "").lower()
        if host not in local_names:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                raise PermissionError("Open PiperTV using the Raspberry Pi's LAN IP address "
                                      "or hostname.") from None
            if not (address.is_private or address.is_loopback or address.is_link_local):
                raise PermissionError("PiperTV accepts connections through local network "
                                      "addresses.")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                raise PermissionError("Cross-origin requests are not allowed.")
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                raise PermissionError("Cross-site requests are not allowed.")
            if request.mimetype != "application/json":
                raise ValueError("Request body must be application/json.")

    @app.after_request
    def add_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = CSP
        return response

    @app.errorhandler(PermissionError)
    def forbidden(error):
        return jsonify(error=str(error)), 403

    @app.errorhandler(ValueError)
    @app.errorhandler(TypeError)
    def bad_request(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(KeyError)
    def not_found(error):
        return jsonify(error=str(error.args[0]) if error.args else "Not found"), 404

    @app.errorhandler(RuntimeError)
    def conflict(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(OSError)
    def storage_error(_error):
        return jsonify(error="Could not read or write the recordings file. "
                             "Check folder permissions and free disk space."), 500

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    def body() -> dict:
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object.")
        return value

    def require_remote() -> RemoteControl:
        if remote is None:
            raise KeyError("Desktop control is not running on this server.")
        return remote

    # --- pages ---------------------------------------------------------------

    @app.get("/")
    @app.get("/index.html")
    def studio():
        return app.send_static_file("index.html")

    @app.get("/tv")
    @app.get("/tv.html")
    def tv():
        return app.send_static_file("tv.html")

    @app.get("/<any('app.js', 'style.css', 'tv.js', 'tv.css'):name>")
    def asset(name):
        return app.send_static_file(name)

    # --- recordings ----------------------------------------------------------

    @app.get("/api/state")
    def state():
        return jsonify(recorder.state())

    @app.get("/api/health")
    def health():
        result = recorder.health()
        if remote is not None:
            result["control"] = remote.health()
        return jsonify(result)

    @app.post("/api/captures")
    def start_capture():
        return jsonify(recorder.start(body())), 202

    @app.get("/api/captures/<job_id>")
    def get_capture(job_id):
        return jsonify(recorder.get(job_id))

    @app.post("/api/captures/<job_id>/cancel")
    def cancel_capture(job_id):
        body()
        return jsonify(recorder.cancel(job_id))

    @app.put("/api/buttons/<button_id>")
    def rename_button(button_id):
        store.rename(button_id, body().get("label"))
        return state()

    @app.delete("/api/buttons/<button_id>/samples/<sample>")
    def delete_sample(button_id, sample):
        store.delete_sample(button_id, int(sample))
        if remote is not None:
            remote.reload_recordings()
        return state()

    @app.get("/api/export")
    def export_all():
        text = json.dumps(store.snapshot(), indent=2, ensure_ascii=False, allow_nan=False)
        response = Response(text + "\n", content_type="application/json; charset=utf-8")
        response.headers["Content-Disposition"] = 'attachment; filename="pipertv-recordings.json"'
        return response

    @app.get("/api/export/<button_id>")
    def export_sample(button_id):
        if request.args.get("format", "irctl") != "irctl":
            raise ValueError("Supported sample export format: irctl")
        text = store.irctl(button_id, int(request.args.get("sample", "-1")))
        response = Response(text, content_type="text/plain; charset=utf-8")
        response.headers["Content-Disposition"] = f'attachment; filename="{button_id}.ir"'
        return response

    # --- settings ------------------------------------------------------------

    def role_state() -> dict:
        bindings = store.roles()
        return {"bindings": bindings, "roles": RoleMap(bindings).describe(),
                "suggested": dict(SUGGESTED)}

    @app.get("/api/roles")
    def get_roles():
        return jsonify(role_state())

    @app.put("/api/roles/<role>")
    def bind_role(role):
        # No button means the role goes back to its own key.
        store.set_role(role, body().get("button"))
        if remote is not None:
            remote.reload_roles()
        return jsonify(role_state())

    def pointer_state() -> dict:
        return {"settings": validate_pointer(store.pointer()),
                "defaults": dict(POINTER_DEFAULTS),
                "limits": {name: list(bounds) for name, bounds in POINTER_LIMITS.items()},
                "drives": list(DRIVES)}

    @app.get("/api/pointer")
    def get_pointer():
        return jsonify(pointer_state())

    @app.put("/api/pointer")
    def set_pointer():
        store.set_pointer(body())
        if remote is not None:
            remote.reload_pointer()
        return jsonify(pointer_state())

    def window_state() -> dict:
        return {"settings": validate_window(store.window()),
                "defaults": dict(WINDOW_DEFAULTS),
                "limits": {name: list(bounds) for name, bounds in WINDOW_LIMITS.items()}}

    @app.get("/api/window")
    def get_window():
        return jsonify(window_state())

    @app.put("/api/window")
    def set_window():
        store.set_window(body())
        if remote is not None:
            remote.reload_window()  # reopens the interface in the new shape
        return jsonify(window_state())

    def receiver_state() -> dict:
        lines, error = gpio_lines()
        held = kernel_line(lines)
        for line in lines:
            # A pin held by the kernel's receiver is usable: we read it through /dev/lirc0.
            line["kernel"] = line is held
            line["free"] = not line["used"] or line["consumer"] == CONSUMER or line["kernel"]
        return {"pin": pin, "default_pin": DEFAULT_PIN,
                "reading": describe(resolve(pin, lines, lirc=device)),
                "automatic": describe(resolve(AUTO, lines, lirc=device)),
                "config": str(config_path),
                "kernel": {"device": device,
                           "gpio": held["gpio"] if held else None,
                           "header_pin": held["header_pin"] if held else None},
                "lines": lines, "error": error,
                "listening": remote.controller.health() if remote is not None else None}

    @app.get("/api/receiver")
    def get_receiver():
        return jsonify(receiver_state())

    @app.put("/api/receiver")
    def set_receiver():
        # Takes effect at once and is saved to pipertv.conf for the next start.
        nonlocal pin
        values = body()
        if set(values) - {"pin"}:
            raise ValueError('The receiver is set by its pin alone: {"pin": 18} or '
                             '{"pin": "auto"}.')
        chosen = validate_pin(values.get("pin", AUTO))
        lines, _error = gpio_lines()
        line = next((line for line in lines if line["gpio"] == chosen), None)
        if (line and line["used"] and line["consumer"] != CONSUMER
                and not (line["consumer"] or "").startswith(KERNEL_RECEIVER)):
            holder = line["consumer"] or "another driver"
            raise RuntimeError(f"GPIO{chosen} is already in use by {holder}. Choose a free pin.")
        pin = write_pin(chosen, config_path)
        receiver = resolve(pin, lines, lirc=device)
        recorder.use(receiver)
        if remote is not None:
            remote.use_receiver(receiver)
        LOG.info("The IR receiver is now %s", describe(receiver))
        return jsonify(receiver_state())

    # --- desktop control -----------------------------------------------------

    @app.get("/api/control")
    def control_state():
        return jsonify(require_remote().snapshot())

    @app.post("/api/control/mode")
    def choose_mode():
        # The session id ties the choice to one visit, so a stale page can't
        # turn control back on after the TV switched away.
        values = body()
        return jsonify(require_remote().choose(values.get("mode"), values.get("session_id")))

    @app.post("/api/control/manual")
    def confirm_manually():
        return jsonify(require_remote().manual(body().get("confirmed")))

    @app.post("/api/control/stop")
    def stop_control():
        body()
        return jsonify(require_remote().stop())

    # --- the TV interface ----------------------------------------------------

    @app.post("/api/tv/launch")
    def tv_launch():
        values = body()
        return jsonify(require_remote().launch(values.get("service"), values.get("session_id")))

    @app.post("/api/tv/close")
    def tv_close():
        body()
        return jsonify(require_remote().stop_service())

    @app.post("/api/tv/leave")
    def tv_leave():
        body()
        return jsonify(require_remote().leave())

    @app.post("/api/tv/keyboard")
    def tv_keyboard():
        show = body().get("show", True)
        control = require_remote()
        return jsonify(control.open_keyboard() if show else control.close_keyboard())

    @app.post("/api/tv/interface")
    def show_tv_interface():
        return jsonify(require_remote().show_interface())

    @app.get("/api/tv/events")
    def tv_events():
        after = request.args.get("after", "0")
        if not after.isdigit():
            raise ValueError("The last seen press must be a whole number, zero or more.")
        return jsonify(require_remote().events(int(after)))

    return app


def setup_logging(level=logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # The TV page polls several times a second; don't log every request.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run PiperTV on this Raspberry Pi.")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--device", default=LIRC, help="kernel IR receiver (default: %(default)s)")
    parser.add_argument("--demo", action="store_true", help="simulated signals, no hardware")
    parser.add_argument("--data", type=Path, help="recordings file (default: data/recordings.json)")
    parser.add_argument("--browser", help="browser that opens services on the TV (default: chromium)")
    parser.add_argument("--no-control", action="store_true",
                        help="only record buttons; don't let the remote drive the desktop")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    setup_logging()
    try:
        app = create_app(args.data, args.device, args.demo,
                         control=not args.demo and not args.no_control,
                         browser=args.browser, port=args.port)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"PiperTV: {exc}\n")
    recorder = app.extensions["recorder"]
    remote = app.extensions["remote"]
    print(f"PiperTV{' (demo)' if args.demo else ''} is running: "
          f"open http://<pi-address>:{args.port} in a browser.", flush=True)
    print(f"Recordings: {recorder.store.path}", flush=True)

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    previous = {signum: signal.signal(signum, stop) for signum in (signal.SIGTERM, signal.SIGHUP)}
    try:
        app.run(host=args.host, port=args.port, threaded=True, processes=1,
                debug=False, use_debugger=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        recorder.close()
        if remote is not None:
            remote.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    main()
