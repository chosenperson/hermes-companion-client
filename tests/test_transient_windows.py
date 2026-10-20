"""Transient UI must not replace the accepted normal frame, even on demand."""
import ctypes
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from companion.app import Companion, DEFAULT_CONFIG
from companion.capture import Capturer, Foreground, is_transient_window, window_attributes


TRANSIENTS = [
    Foreground(process='XboxPcTray.exe', rect=(3060, 1302, 3360, 1613)),
    Foreground(process='explorer.exe', window_class='NotifyIconOverflowWindow'),
    Foreground(process='browser.exe', window_class='#32768'),
    Foreground(process='browser.exe', window_class='tooltips_class32'),
    Foreground(process='browser.exe', window_class='Xaml_WindowedPopupClass'),
    Foreground(process='browser.exe', ex_style=0x80),
    Foreground(process='browser.exe', style=0x80000000, has_owner=True),
]


@pytest.mark.parametrize('window', TRANSIENTS)
def test_transient_classification(window):
    assert is_transient_window(window)


@pytest.mark.parametrize('window', [
    Foreground(process='notepad.exe', rect=(10, 10, 210, 110)),
    Foreground(process='browser.exe', window_class='Chrome_WidgetWin_1', style=0x00C00000),
    Foreground(process='game.exe', style=0x80000000),  # Borderless game, unowned.
    Foreground(process='game.exe', style=0x80000000, has_owner=True, ex_style=0x40000),
    Foreground(process='editor.exe', style=0x80C00000, has_owner=True),  # Captioned dialog.
    Foreground(process='XboxPcApp.exe', window_class='WinUIDesktopWin32WindowClass'),
])
def test_regular_daily_and_windowed_apps_remain_eligible(window):
    assert not is_transient_window(window)


@pytest.mark.parametrize('popup', TRANSIENTS)
def test_popup_preserves_accepted_frame_and_pending_request(monkeypatch, tmp_path, popup):
    app = Companion(dict(DEFAULT_CONFIG, server_url='http://localhost', token='test',
                         proxy='direct', last_upload_dir=str(tmp_path)))
    app.state = {'enabled': True, 'daily': True, 'want_frame_after': 999, 'client': {}}
    normal = Foreground(title='normal daily window', process='browser.exe', rect=(0, 0, 100, 100))
    app.capturer = Mock()
    app.capturer.grab.return_value = Image.new('RGB', (100, 100), 'red')
    app.capturer.crop_to_window.side_effect = lambda image, fg: image
    app.api.upload = Mock(return_value={'accepted': True, 'frame_ts': 123})
    monkeypatch.setattr('companion.app.foreground', lambda: normal)
    monkeypatch.setattr('companion.app.idle_seconds', lambda: 0)
    app._capture_once()
    meta_path = tmp_path / 'last-upload.json'
    evidence = meta_path.read_bytes()
    thumb = app.decider.last_thumb
    assert app.stats['uploads'] == 1
    app.api.upload.reset_mock()
    app.capturer.grab.reset_mock()
    app.decider.hotkey_pending = True
    monkeypatch.setattr('companion.app.foreground', lambda: popup)
    app._capture_once()
    app.api.upload.assert_not_called()
    app.capturer.grab.assert_not_called()
    assert app._scene(popup) is None
    assert app.decider.hotkey_pending
    assert app.decider.last_upload_server_ts == 123
    assert app.decider.last_thumb is thumb
    assert meta_path.read_bytes() == evidence
    # Returning to a normal daily window fulfills the waiting look request.
    monkeypatch.setattr('companion.app.foreground', lambda: normal)
    app.capturer.grab.return_value = Image.new('RGB', (100, 100), 'blue')
    app.api.upload.return_value = {'accepted': True, 'frame_ts': 124}
    app._capture_once()
    assert app.stats['uploads'] == 2 and not app.decider.hotkey_pending
    assert json.loads(meta_path.read_text(encoding='utf-8'))['process'] == 'browser.exe'


def test_popup_appearing_during_capture_discards_cached_pixels(monkeypatch):
    app = Companion(dict(DEFAULT_CONFIG, server_url='http://localhost', token='test', proxy='direct'))
    app.state = {'enabled': True, 'daily': True, 'want_frame_after': 1, 'client': {}}
    normal = Foreground(title='same', process='browser.exe', rect=(0, 0, 100, 100))
    popup = Foreground(title='same', process='browser.exe', rect=normal.rect, ex_style=0x80)
    app.capturer = Mock()
    app.capturer.grab.return_value = Image.new('RGB', (100, 100))
    app.capturer.crop_to_window.side_effect = lambda image, fg: image
    app.api.upload = Mock()
    monkeypatch.setattr('companion.app.foreground', Mock(side_effect=[normal, popup]))
    monkeypatch.setattr('companion.app.idle_seconds', lambda: 0)
    app._capture_once()
    app.api.upload.assert_not_called()
    app.capturer.discard_last_frame.assert_called_once()
    cap = Capturer.__new__(Capturer)
    cap._last = Image.new('RGB', (1, 1))
    cap.discard_last_frame()
    assert cap._last is None


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Win32 metadata integration')
def test_actual_hidden_owned_window_flags():
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    create = user.CreateWindowExW
    create.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    create.restype = wintypes.HWND
    user.DestroyWindow.argtypes = [wintypes.HWND]
    handles = []
    try:
        # Test-owned hidden windows: no foreground/input or user-app changes.
        owner = create(0, 'STATIC', 'test owner', 0x00CF0000, 0, 0, 300, 200, None, None, None, None)
        assert owner
        handles.append(owner)
        popup = create(0x80, 'STATIC', 'test popup', 0x80000000, 0, 0, 100, 80, owner, None, None, None)
        assert popup
        handles.append(popup)
        style, ex_style, has_owner = window_attributes(popup)
        assert style & 0x80000000 and ex_style & 0x80 and has_owner
        assert is_transient_window(Foreground(style=style, ex_style=ex_style, has_owner=has_owner))
        style, ex_style, has_owner = window_attributes(owner)
        assert not is_transient_window(Foreground(style=style, ex_style=ex_style, has_owner=has_owner))
    finally:
        for handle in reversed(handles):
            user.DestroyWindow(handle)
