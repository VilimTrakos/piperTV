"""Flask application running directly on a Raspberry Pi with a gpio-ir receiver."""

from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import signal
import socket
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

from .control import RemoteControl
from .learner import Workbench
from .roles import SUGGESTED, RoleMap
from .storage import RecordingStore

STATIC = Path(__file__).parent / "static"


def create_app(data: str | Path | None = None, device: str = "/dev/lirc0",
               demo: bool = False, workbench: Workbench | None = None,
               control: bool = False, remote: RemoteControl | None = None,
               browser: str | None = None, port: int = 8765) -> Flask:
    """Build one application and one capture manager, shared by all browsers.

    Desktop control is opt-in: it opens real devices and runs a detector thread,
    so it stays off unless this Pi is meant to be driven by its own remote.
    """
    if workbench is None:
        path = data or Path("data/demo-recordings.json" if demo else "data/recordings.json")
        store = RecordingStore(path)
        if remote is None and control and not demo:
            # The port identifies this app's own interface window, which the
            # remote can ask to close.
            remote = RemoteControl(store, device=device, browser=browser, port=port)
        # The gate stands the desktop down while a button is being learned.
        workbench = Workbench(store, device=device, demo=demo, gate=remote)
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.config.update(MAX_CONTENT_LENGTH=32768, JSON_SORT_KEYS=False)
    app.json.sort_keys = False
    app.extensions["pipertv"] = workbench
    app.extensions["pipertv_control"] = remote
    if remote is not None:
        remote.start()
    local_names = {"localhost", socket.gethostname().lower(), socket.getfqdn().lower()}
    local_names |= {name + ".local" for name in tuple(local_names) if "." not in name}

    @app.before_request
    def guard_request():
        # This app is intended for the Pi's own hostname or a LAN IP. Reject
        # unrelated internet Host names so DNS rebinding cannot read recordings.
        hostname = (urlsplit(request.host_url).hostname or "").lower()
        if hostname not in local_names:
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError:
                raise PermissionError("Open PiperTV using the Raspberry Pi's LAN IP address or hostname.")
            if not (address.is_private or address.is_loopback or address.is_link_local):
                raise PermissionError("PiperTV accepts connections through local network addresses.")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                raise PermissionError("Cross-origin requests are not allowed.")
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                raise PermissionError("Cross-site requests are not allowed.")
            if request.mimetype != "application/json":
                raise ValueError("Request body must be application/json.")

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    def body() -> dict:
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object.")
        return value

    def local_health() -> dict:
        result = workbench.health()
        if result.get("mode") == "hardware":
            if result.get("device_exists") is False:
                result.update(ok=False, error=f"IR receiver {result.get('device', device)} was not found. "
                              "Connect the receiver, enable the gpio-ir overlay, and reboot the Pi.")
            elif result.get("device_readable") is False:
                result.update(ok=False, error="The IR receiver exists but is not readable. "
                              "Grant your Pi user access to the LIRC device, then restart PiperTV.")
        return result

    @app.errorhandler(PermissionError)
    def permission_error(error):
        return jsonify(error=str(error)), 403

    @app.errorhandler(ValueError)
    @app.errorhandler(TypeError)
    def invalid_request(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(KeyError)
    def unknown_item(error):
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

    @app.get("/")
    @app.get("/index.html")
    def index():
        return app.send_static_file("index.html")

    @app.get("/app.js")
    def javascript():
        return app.send_static_file("app.js")

    @app.get("/style.css")
    def stylesheet():
        return app.send_static_file("style.css")

    # The Piper interface, shown full screen on the Pi's own HDMI output.
    @app.get("/tv")
    @app.get("/tv.html")
    def tv_interface():
        return app.send_static_file("tv.html")

    @app.get("/tv.js")
    def tv_javascript():
        return app.send_static_file("tv.js")

    @app.get("/tv.css")
    def tv_stylesheet():
        return app.send_static_file("tv.css")

    @app.get("/api/state")
    def state():
        return jsonify(workbench.state())

    @app.get("/api/health")
    def health():
        result = local_health()
        if remote is not None:
            result["control"] = remote.health()
        return jsonify(result)

    @app.post("/api/captures")
    def start_capture():
        return jsonify(workbench.start(body())), 202

    @app.get("/api/captures/<job_id>")
    def get_capture(job_id):
        return jsonify(workbench.get(job_id))

    @app.post("/api/captures/<job_id>/cancel")
    def cancel_capture(job_id):
        body()
        return jsonify(workbench.cancel(job_id))

    @app.put("/api/buttons/<button_id>")
    def rename_button(button_id):
        workbench.store.rename(button_id, body().get("label"))
        return state()

    @app.delete("/api/buttons/<button_id>/samples/<sample>")
    def delete_sample(button_id, sample):
        # Existing UI sends a JSON content type with an empty DELETE body.
        workbench.store.delete_sample(button_id, int(sample))
        if remote is not None:
            remote.reload_recordings()
        return state()

    @app.get("/api/export")
    def export_all():
        response = Response(json.dumps(workbench.store.snapshot(), indent=2,
                                       ensure_ascii=False, allow_nan=False) + "\n",
                            content_type="application/json; charset=utf-8")
        response.headers["Content-Disposition"] = 'attachment; filename="pipertv-recordings.json"'
        return response

    @app.get("/api/export/<button_id>")
    def export_sample(button_id):
        if request.args.get("format", "irctl") != "irctl":
            raise ValueError("Supported sample export format: irctl")
        sample = int(request.args.get("sample", "-1"))
        text = workbench.store.irctl(button_id, sample)
        response = Response(text, content_type="text/plain; charset=utf-8")
        response.headers["Content-Disposition"] = f'attachment; filename="{button_id}.ir"'
        return response

    def role_state() -> dict:
        bindings = workbench.store.roles()
        return {"bindings": bindings, "roles": RoleMap(bindings).describe(),
                "suggested": dict(SUGGESTED)}

    @app.get("/api/roles")
    def get_roles():
        return jsonify(role_state())

    @app.put("/api/roles/<role>")
    def bind_role(role):
        # Bindings belong to the recordings library, not to the control gate,
        # so the studio can arrange them whether or not the remote is driving
        # this desktop. Sending no button releases the role back to its own key.
        workbench.store.set_role(role, body().get("button"))
        if remote is not None:
            remote.reload_roles()
        return jsonify(role_state())

    def desktop() -> RemoteControl:
        if remote is None:
            raise KeyError("Desktop control is not running on this server.")
        return remote

    @app.get("/api/control")
    def control_state():
        return jsonify(desktop().snapshot())

    @app.post("/api/control/mode")
    def choose_mode():
        # The session id ties the choice to one visit to the Pi's input, so a
        # stale browser cannot re-enable control after the TV switched away.
        values = body()
        return jsonify(desktop().choose(values.get("mode"), values.get("session_id")))

    @app.post("/api/control/manual")
    def confirm_manually():
        return jsonify(desktop().manual(body().get("confirmed")))

    @app.post("/api/control/stop")
    def stop_control():
        body()
        return jsonify(desktop().stop())

    @app.post("/api/tv/launch")
    def tv_launch():
        # The interface names the visit it is showing, exactly as the mode
        # choice does, so a stale page cannot open something on a TV that has
        # since been switched to another input.
        values = body()
        return jsonify(desktop().launch(values.get("service"), values.get("session_id")))

    @app.post("/api/tv/close")
    def tv_close():
        body()
        return jsonify(desktop().stop_service())

    @app.get("/api/tv/events")
    def tv_events():
        # The page reports the last press it saw and receives what followed.
        after = request.args.get("after", "0")
        if not after.isdigit():
            raise ValueError("The last seen press must be a whole number, zero or more.")
        return jsonify(desktop().events(int(after)))

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: all IPv4 interfaces)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--device", default="/dev/lirc0")
    parser.add_argument("--demo", action="store_true", help="Test the UI without GPIO hardware")
    parser.add_argument("--data", type=Path, help="Recordings JSON path (demo uses a separate default file)")
    parser.add_argument("--browser", help="Browser command used to open a service on the TV "
                        "(default: chromium)")
    parser.add_argument("--no-control", action="store_true",
                        help="Learn remote buttons only; do not let the remote drive this desktop")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        app = create_app(args.data, args.device, args.demo,
                         control=not args.demo and not args.no_control,
                         browser=args.browser, port=args.port)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"PiperTV: {exc}\n")
    workbench = app.extensions["pipertv"]
    remote = app.extensions["pipertv_control"]
    print(f"PiperTV {'DEMO' if args.demo else 'IR learner'} running on this Raspberry Pi.", flush=True)
    print(f"Open http://<raspberry-pi-ip>:{args.port} from your PC browser.", flush=True)
    print(f"Recordings: {workbench.store.path}", flush=True)
    def stop(_signum, _frame):
        raise KeyboardInterrupt

    previous_handlers = {}
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous_handlers[signum] = signal.signal(signum, stop)
    try:
        app.run(host=args.host, port=args.port, threaded=True, processes=1,
                debug=False, use_debugger=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        workbench.close()
        if remote is not None:
            remote.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    main()
