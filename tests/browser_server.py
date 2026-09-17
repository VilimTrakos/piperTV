import signal
import sys
import tempfile
from pathlib import Path
from flask import request, jsonify
from werkzeug.serving import make_server
from pipertv.app import create_app
from pipertv.storage import RecordingStore
from pipertv.control import RemoteControl
from tests.test_control import (FakeMonitor, FakeController, FakeTargets, FakeDesktop,
                               FakeLauncher, FakeInterface, FakeKeys, FakeWatcher)

temporary = tempfile.TemporaryDirectory(prefix='pipertv-browser-')
data = str(Path(temporary.name) / 'recordings.json')
monitor = FakeMonitor()
controller = FakeController()
controller.reload_recordings = lambda: None
remote = RemoteControl(RecordingStore(Path(data)), screen=(1920, 1080),
                       monitor=monitor, controller=controller,
                       targets=FakeTargets(), desktop=FakeDesktop(),
                       launcher=FakeLauncher(), interface=FakeInterface(), keys=FakeKeys(),
                       watcher=FakeWatcher())
app = create_app(data=data, demo=True, remote=remote)

@app.post('/test/source')
def source():
    monitor.state = request.get_json()['state']
    remote._tick()
    return jsonify(remote.snapshot())

@app.post('/test/press')
def press():
    remote._press(request.get_json()['button'])
    return jsonify(remote.events())

def stop(*_):
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, stop)
server = make_server('127.0.0.1', 0, app, threaded=True)
print(f'PIPERTV_TEST_PORT={server.server_port}', flush=True)
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    server.server_close()
    app.extensions['pipertv'].close()
    remote.close()
    temporary.cleanup()
