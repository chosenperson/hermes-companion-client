"""Watch cadence, native coordinate ABI, privacy and accepted-payload evidence."""
import ctypes
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from companion import capture
from companion.app import Companion, DEFAULT_CONFIG
from companion.capture import Foreground


def watch_app(monkeypatch):
    app = Companion(dict(DEFAULT_CONFIG, server_url='http://localhost', token='test',
                         proxy='direct', watch_monitor=2))
    app.state = {'enabled': True, 'watch': True, 'want_frame_after': 0,
                 'client': {'watch_upload_s': 600, 'watch_static_stop_s': 900, 'watch_max_s': 10800}}
    app.watch_capturer = Mock()
    app.watch_capturer.bounds = (-2560, -1440, 0, 0)
    app.watch_capturer.grab.return_value = Image.new('RGB', (160, 90), 'red')
    app.api.upload = Mock(return_value={'accepted': True, 'frame_ts': 123})
    clock = [1000.0]
    monkeypatch.setattr('companion.app.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('companion.app.foreground', lambda: Foreground(process='browser.exe'))
    monkeypatch.setattr('companion.app.process_at', lambda x, y: 'player.exe')
    return app, clock


def test_watch_real_interval_and_static_deadline(monkeypatch):
    app, clock = watch_app(monkeypatch)
    app._watch_once()
    assert app.api.upload.call_count == 1
    for minute in range(1, 11):
        clock[0] = 1000 + 60 * minute
        app.watch_capturer.grab.return_value = Image.new('RGB', (160, 90), 'blue' if minute % 2 else 'red')
        app._watch_once()
        assert app.api.upload.call_count == (1 if minute < 10 else 2)
    app.control = Mock()
    for minute in range(1, 15):
        clock[0] = 1600 + minute * 60
        app._watch_once()
        app.control.assert_not_called()
    clock[0] = 2500
    app._watch_once()
    app.control.assert_called_once_with('watch_off', source='auto')


@pytest.mark.parametrize('process', ['', 'WeChat.exe', 'Weixin.exe'])
def test_private_or_unknown_center_is_not_captured(monkeypatch, process):
    app, clock = watch_app(monkeypatch)
    monkeypatch.setattr('companion.app.process_at', lambda x, y: process)
    app._watch_once()
    app.watch_capturer.grab.assert_not_called()
    app.api.upload.assert_not_called()


def test_watch_locked_and_center_switch_do_not_upload(monkeypatch):
    app, clock = watch_app(monkeypatch)
    monkeypatch.setattr('companion.app.foreground', lambda: Foreground(process='LockApp.exe'))
    app._watch_once()
    app.watch_capturer.grab.assert_not_called()
    monkeypatch.setattr('companion.app.foreground', lambda: Foreground(process='browser.exe'))
    monkeypatch.setattr('companion.app.process_at', Mock(side_effect=['player.exe', 'WeChat.exe']))
    capturer = app.watch_capturer
    app._watch_once()
    app.api.upload.assert_not_called()
    capturer.close.assert_called_once()
    assert app.watch_capturer is None


def test_watch_rejection_retry_and_evidence_are_separate_from_primary(monkeypatch, tmp_path):
    app, clock = watch_app(monkeypatch)
    app.config['last_upload_dir'] = str(tmp_path)
    app.api.upload.return_value = {'accepted': False, 'frame_ts': 123}
    app._watch_once()
    assert app.watch['last_upload'] == 0 and app.stats['uploads'] == 0
    assert not list(tmp_path.rglob('*.jpg'))
    app.api.upload.return_value['accepted'] = True
    clock[0] += 3
    app._watch_once()
    assert app.watch['last_upload'] == clock[0] and app.stats['uploads'] == 1
    payload = app.api.upload.call_args.args[0]
    assert app.api.upload.call_args.args[5] == 'watch'
    assert app.api.upload.call_args.kwargs['source'].endswith('-side')
    meta = json.loads((tmp_path / 'watch' / 'last-upload.json').read_text(encoding='utf-8'))
    assert meta['monitor'] == 2 and meta['scene'] == 'watch'
    assert meta['sha256'] == hashlib.sha256(payload).hexdigest()
    assert (tmp_path / 'watch' / meta['image']).read_bytes() == payload
    app._save_last_upload(b'primary', Foreground(), 'daily', 'timer', 124)
    assert (tmp_path / 'watch' / meta['image']).exists()
    app._accept_state(dict(app.state, watch=False))
    app._accept_state(dict(app.state, watch=True))
    clock[0] += 3
    app._watch_once()
    assert app.stats['uploads'] == 2  # A newly enabled watch session gets its own first frame.


def test_max_duration_is_enforced_even_with_private_center(monkeypatch):
    app, clock = watch_app(monkeypatch)
    app.watch.update(since=1.0, last_change=clock[0])
    clock[0] = 10801
    app.control = Mock()
    monkeypatch.setattr('companion.app.process_at', lambda x, y: 'WeChat.exe')
    app._watch_once()
    app.control.assert_called_once_with('watch_off', source='auto')
    app.api.upload.assert_not_called()


def test_process_at_preserves_negative_point_and_64_bit_handles(monkeypatch):
    hwnd, root = 0x100000123, 0x200000456
    def locate(point):
        assert (point.x, point.y) == (-1280, -720)
        return hwnd
    def identify(handle, pointer):
        assert handle == root
        pointer._obj.value = 42
    user = SimpleNamespace(WindowFromPoint=Mock(side_effect=locate),
                           GetAncestor=Mock(return_value=root), GetWindowThreadProcessId=Mock(side_effect=identify))
    monkeypatch.setattr(capture, 'IS_WINDOWS', True)
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(user32=user), raising=False)
    monkeypatch.setattr('psutil.Process', lambda pid: SimpleNamespace(name=lambda: 'player.exe'))
    assert capture.process_at(-1280, -720) == 'player.exe'
    user.GetAncestor.assert_called_once_with(hwnd, 2)
    from ctypes import wintypes
    assert user.WindowFromPoint.restype is wintypes.HWND
    assert user.GetAncestor.restype is wintypes.HWND
