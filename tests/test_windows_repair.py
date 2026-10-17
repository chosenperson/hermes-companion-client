"""Transport/privacy regressions plus an isolated Windows instance-lock check."""
import ctypes
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from PIL import Image

from companion import capture
from companion.app import Api, Companion, DEFAULT_CONFIG, load_config, run_tray
from companion.capture import Capturer, Foreground
from companion.logic import ThrottledReader


def make_app():
    cfg = dict(DEFAULT_CONFIG, server_url='http://localhost', token='test', proxy='direct')
    app = Companion(cfg)
    app.state = {'enabled': True, 'daily': True, 'want_frame_after': 999, 'client': {}}
    return app


def test_requests_framing_and_local_receiver():
    payload = bytes(range(256)) * 160
    prepared = requests.Request('POST', 'http://localhost/frame',
                                data=ThrottledReader(payload, 1e9),
                                headers={'Content-Length': str(len(payload))}).prepare()
    assert prepared.headers['Content-Length'] == str(len(payload))
    assert 'Transfer-Encoding' not in prepared.headers
    stream = ThrottledReader(payload, 1e9)
    stream.read(19)
    assert requests.utils.super_len(stream) == len(payload) - 19
    received = {}

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received['headers'] = dict(self.headers)
            received['body'] = self.rfile.read(int(self.headers['Content-Length']))
            response = json.dumps({'accepted': True, 'frame_ts': 1, 'state': {}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api = Api({'server_url': f'http://127.0.0.1:{server.server_port}',
               'token': 'local-test', 'source': 'test', 'proxy': 'direct'})
    try:
        assert api.upload(payload, 'timer', 0.5, 'test', 1e9, 'daily')['accepted']
        assert received['body'] == payload
        assert 'Transfer-Encoding' not in received['headers']
        assert received['headers']['X-Companion-Scene'] == 'daily'
    finally:
        api.session.close()
        server.shutdown()
        thread.join(3)
        server.server_close()


def test_explicit_proxy_overrides_inherited_settings(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://invalid:1')
    for proxy in ('direct', 'http://localhost:1234'):
        api = Api({'server_url': 'http://localhost', 'token': 'test', 'source': 'test', 'proxy': proxy})
        assert api.session.trust_env is False
        assert api.session.proxies == ({} if proxy == 'direct' else {'http': proxy, 'https': proxy})
        api.session.close()


def test_offline_diagnostics_do_not_require_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv('COMPANION_TOKEN', raising=False)
    monkeypatch.delenv('COMPANION_SERVER_URL', raising=False)
    missing = str(tmp_path / 'missing.json')
    assert load_config(missing, require_auth=False)['token'] == ''
    with pytest.raises(SystemExit):
        load_config(missing)


@pytest.mark.parametrize('bounds', [(0, 0, 3840, 2160), (-3840, -2160, 0, 0)])
def test_border_slack_negative_coordinates_and_crop(bounds):
    class Probe(Capturer):
        @property
        def bounds(self):
            return bounds

    cap = Probe.__new__(Probe)
    l, t, r, b = bounds
    assert cap.covers_monitor(Foreground(rect=(l - 8, t - 8, r + 8, b + 8)))
    assert not cap.covers_monitor(Foreground(rect=(l + 100, t, r, b)))
    image = Image.new('RGB', (3840, 2160), 'red')
    cropped = cap.crop_to_window(image, Foreground(rect=(l + 5, t + 7, l + 25, t + 37)))
    assert cropped.size == (20, 30)  # Tiny windows must never fall back to the full desktop.
    assert cap.crop_to_window(image, Foreground(rect=(l - 8, t - 8, r + 8, b + 8))).size == image.size
    with pytest.raises(ValueError):
        cap.crop_to_window(image, Foreground(rect=(r + 1, t, r + 10, t + 20)))
    with pytest.raises(ValueError):
        cap.crop_to_window(image, Foreground())


def test_whitelisted_game_on_other_monitor_is_not_a_scene(monkeypatch):
    app = make_app()
    app.config['allow_processes'] = ['game.exe']
    app.capturer = Mock()
    app.capturer.window_on_monitor.return_value = False
    monkeypatch.setattr('companion.app.idle_seconds', lambda: 0)
    assert app._scene(Foreground(process='game.exe', rect=(4000, 0, 5000, 1000))) is None


def test_lock_screen_cannot_bypass_filter_with_pending_request(monkeypatch):
    app = make_app()
    app.decider.hotkey_pending = True
    monkeypatch.setattr('companion.app.foreground', lambda: Foreground(process='LockApp.exe', rect=(0, 0, 100, 100)))
    app._ensure_capturer = Mock(side_effect=AssertionError('Must not initialize capture on lock screen'))
    app._capture_once()
    assert app.stats['uploads'] == 0


@pytest.mark.parametrize('accepted', [True, False])
def test_switched_foreground_and_rejected_frames_are_not_counted(monkeypatch, accepted):
    app = make_app()
    fg = Foreground(title='test', process='game.exe', rect=(0, 0, 100, 100))
    app.capturer = Mock()
    app.capturer.grab.return_value = Image.new('RGB', (100, 100))
    app.capturer.crop_to_window.side_effect = lambda image, window: image
    app._scene = Mock(return_value='game')
    app.api.upload = Mock(return_value={'accepted': accepted, 'frame_ts': 123, 'state': app.state})
    monkeypatch.setattr('companion.app.foreground', Mock(side_effect=[fg, Foreground(process='chat.exe')]))
    app._capture_once()
    app.api.upload.assert_not_called()
    monkeypatch.setattr('companion.app.foreground', lambda: fg)
    app._capture_once()
    assert app.stats['uploads'] == int(accepted)
    assert app.decider.last_upload_server_ts == (123 if accepted else 0)


@pytest.mark.parametrize('last,now,expected', [(0x7FFFFFF0, 0x80000010, .032),
                                            (0xFFFFFFF0, 0x10, .032)])
def test_idle_counter_signed_boundary_and_wrap(monkeypatch, last, now, expected):
    def get_input(pointer):
        pointer._obj.dwTime = last
        return 1

    kernel = SimpleNamespace(GetTickCount=Mock(return_value=now))
    monkeypatch.setattr(capture, 'IS_WINDOWS', True)
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(user32=SimpleNamespace(GetLastInputInfo=get_input),
                                                       kernel32=kernel), raising=False)
    assert capture.idle_seconds() == pytest.approx(expected)


def test_failed_input_probe_is_not_active(monkeypatch):
    monkeypatch.setattr(capture, 'IS_WINDOWS', True)
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(user32=SimpleNamespace(GetLastInputInfo=lambda _: 0)), raising=False)
    assert capture.idle_seconds() == float('inf')


def test_tray_failure_does_not_stop_future_updates(caplog):
    app = make_app()
    app.on_change = Mock(side_effect=ValueError('broken tray'))
    app._set_status('network failed')
    assert '托盘刷新失败' in caplog.text
    app.on_change = Mock()
    app._set_status('network recovered')
    app.on_change.assert_called_once()


def test_real_tray_refresh_caps_utf16_tooltip(monkeypatch):
    app = make_app()
    long_message = '上传失败：' + '\U0001f600' * 160
    icons = []

    class Icon:
        def __init__(self, *args):
            icons.append(self)

        @property
        def title(self):
            return self._title

        @title.setter
        def title(self, text):
            if len(text.encode('utf-16-le')) > 254:
                raise ValueError('Tooltip exceeded Windows capacity')
            self._title = text

        def update_menu(self):
            pass

        def run(self):
            app._set_status(long_message)

    class Menu:
        SEPARATOR = object()

        def __init__(self, *args):
            pass

    fake = SimpleNamespace(Icon=Icon, Menu=Menu, MenuItem=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, 'pystray', fake)
    run_tray(app)
    assert app.status == long_message
    assert len(icons[0].title.encode('utf-16-le')) <= 254


@pytest.mark.skipif(sys.platform != 'win32', reason='Uses actual Windows named mutex')
def test_duplicate_client_is_blocked_in_same_session(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    code = 'from companion.app import acquire_instance_lock; print(acquire_instance_lock(), flush=True)'
    first = subprocess.Popen([sys.executable, '-c', code + '; import time; time.sleep(20)'],
                             cwd=tmp_path, env=env, stdout=subprocess.PIPE, text=True)
    try:
        assert first.stdout.readline().strip() == 'True'
        assert subprocess.check_output([sys.executable, '-c', code], cwd=tmp_path, env=env, text=True).strip() == 'False'
    finally:
        first.terminate()
        first.wait(5)
        first.stdout.close()
    assert subprocess.check_output([sys.executable, '-c', code], cwd=tmp_path, env=env, text=True).strip() == 'True'
