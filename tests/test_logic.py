import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from companion.logic import (Decider, ThrottledReader, change_ratio, encode_jpeg,  # noqa: E402
                             process_allowed, thumbnail)

STATE = {"enabled": True, "mode": "auto", "want_frame_after": 0,
         "client": {"min_change": 0.12, "min_upload_interval_s": 15, "timer_upload_s": 120,
                    "passive_upload_s": 60}}


def test_change_ratio():
    black = thumbnail(Image.new("RGB", (3840, 2160), "black"))
    white = thumbnail(Image.new("RGB", (3840, 2160), "white"))
    assert change_ratio(None, black) == 1.0
    assert change_ratio(black, black) == 0.0
    assert change_ratio(black, white) == 1.0


def test_encode_downscales_4k():
    data = encode_jpeg(Image.new("RGB", (3840, 2160), (40, 80, 120)), 1600, 70)
    img = Image.open(io.BytesIO(data))
    assert img.size == (1600, 900) and data[:3] == b"\xff\xd8\xff"


def test_process_filter():
    deny = ["WeChat.exe", "QQ.exe"]
    assert not process_allowed("wechat.exe", [], deny)
    assert process_allowed("eldenring.exe", [], deny)
    assert not process_allowed("chrome.exe", ["eldenring.exe"], deny)
    assert process_allowed("EldenRing.exe", ["eldenring.exe"], deny)


def test_decider_rules():
    d = Decider()
    assert d.decide(100, dict(STATE, enabled=False), 1.0) is None
    assert d.decide(100, STATE, 1.0) == "change"          # 第一张
    d.uploaded(100, None, 1000.0, "change")
    assert d.decide(105, STATE, 0.5) is None              # 未到最小上传间隔
    assert d.decide(120, STATE, 0.5) == "change"
    assert d.decide(120, STATE, 0.05) is None             # 变化不够
    assert d.decide(221, STATE, 0.0) == "timer"           # 定时兜底
    assert d.decide(101, dict(STATE, want_frame_after=1001.0), 0.0) == "request"
    d.hotkey_pending = True
    assert d.decide(101, STATE, 0.0) == "hotkey"
    d.uploaded(101, None, 1002.0, "hotkey")
    assert not d.hotkey_pending
    passive = dict(STATE, mode="passive")
    assert d.decide(130, passive, 0.5) is None
    assert d.decide(162, passive, 0.5) == "timer"
    assert d.decide(162, passive, 0.0) is None            # 画面没动就不传


def test_quiet_period_skips_auto_uploads_only():
    d = Decider()
    d.update_state(100, dict(STATE, quiet_for_s=150))
    assert d.decide(120, STATE, 1.0) is None              # 冷却中，变化再大也不传
    assert d.decide(260, STATE, 1.0) == "change"          # 静默期过后恢复
    assert d.decide(120, dict(STATE, want_frame_after=1.0), 0.0) == "request"
    d.hotkey_pending = True
    assert d.decide(120, STATE, 0.0) == "hotkey"


def test_throttled_reader_paces_upload():
    slept = []
    clock = [0.0]

    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    reader = ThrottledReader(b"x" * 200_000, kbps=800, clock=lambda: clock[0], sleep=sleep)
    out = b""
    while chunk := reader.read(8192):
        out += chunk
    assert out == b"x" * 200_000 and len(reader) == 200_000
    assert abs(sum(slept) - 200_000 / 100_000) < 0.01     # 800kbps = 100KB/s → 2 秒


@pytest.mark.skipif(not os.environ.get("COMPANION_RECEIVER"), reason="需要指向服务端 receiver.py")
def test_end_to_end_with_receiver(tmp_path):
    """COMPANION_RECEIVER=/path/to/receiver.py 时，起真实接收端走一遍上传。"""
    import json
    import socket

    from companion.app import Api

    port = socket.socket()
    port.bind(("127.0.0.1", 0))
    number = port.getsockname()[1]
    port.close()
    env = dict(os.environ, HERMES_HOME=str(tmp_path), COMPANION_PORT=str(number))
    proc = subprocess.Popen([sys.executable, os.environ["COMPANION_RECEIVER"]], env=env)
    try:
        for _ in range(50):
            if (tmp_path / "companion" / "config.json").exists():
                break
            time.sleep(0.1)
        time.sleep(0.3)
        token = json.loads((tmp_path / "companion" / "config.json").read_text())["token"]
        api = Api({"server_url": f"http://127.0.0.1:{number}/companion/v1", "token": token,
                   "source": "test/4k", "proxy": ""})
        assert api.state()["enabled"] is False
        assert api.control("start")["enabled"] is True
        jpeg = encode_jpeg(Image.new("RGB", (3840, 2160), (200, 30, 30)), 1600, 70)
        result = api.upload(jpeg, "hotkey", 0.5, "艾尔登法环", kbps=4000)
        assert result["accepted"] and result["frame_ts"]
        meta = json.loads((tmp_path / "companion" / "frames" / "test_4k.json").read_text())
        assert meta["title"] == "艾尔登法环" and meta["bytes"] == len(jpeg)
        assert api.control("stop")["enabled"] is False
    finally:
        proc.terminate()
        proc.wait(5)


def test_looks_like_game():
    from companion.logic import NON_GAME_PROCESSES, looks_like_game
    deny = ["WeChat.exe"]
    assert looks_like_game("eldenring.exe", True, [], deny, NON_GAME_PROCESSES)
    assert not looks_like_game("eldenring.exe", False, [], deny, NON_GAME_PROCESSES)   # 没铺满屏
    assert not looks_like_game("msedge.exe", True, [], deny, NON_GAME_PROCESSES)       # 全屏浏览器
    assert not looks_like_game("WeChat.exe", True, [], deny, NON_GAME_PROCESSES)
    assert looks_like_game("sts.exe", False, ["sts.exe"], deny, NON_GAME_PROCESSES)    # 白名单不要求铺满
    assert not looks_like_game("other.exe", True, ["sts.exe"], deny, NON_GAME_PROCESSES)


def test_auto_session_lifecycle():
    from companion.logic import AutoSession
    a = AutoSession(stop_after_s=600)
    assert a.observe(0, False, "explorer.exe", False) is None
    assert a.observe(3, False, "game.exe", True) == "start"
    assert a.observe(6, True, "game.exe", True) is None
    assert a.observe(60, True, "msedge.exe", False) is None       # 切出去看攻略，不关
    assert a.observe(605, True, "msedge.exe", False) is None      # 距最后见到游戏还不到 600 秒
    assert a.observe(610, True, "msedge.exe", False) == "stop"
    assert not a.active


def test_auto_session_respects_manual_stop_and_manual_start():
    from companion.logic import AutoSession
    a = AutoSession(stop_after_s=600)
    assert a.observe(0, False, "game.exe", True) == "start"
    assert a.observe(3, False, "game.exe", True) == "start"        # 开启没成功（断网）继续尝试
    assert a.observe(6, True, "game.exe", True) is None
    assert a.observe(10, False, "game.exe", True) is None          # 飞书里说了结束：不再自动开
    assert a.observe(20, False, "game.exe", True) is None
    assert a.observe(30, False, "other_game.exe", True) == "start" # 换了游戏重新自动开
    a.manual(31, "stop")
    assert a.observe(40, False, "other_game.exe", True) is None
    assert a.observe(700, False, "explorer.exe", False) is None    # 退出游戏 ≥600 秒，解除
    assert a.observe(703, False, "other_game.exe", True) == "start"
    b = AutoSession(stop_after_s=600)
    b.manual(0, "start")                                           # 手动开的不自动关
    assert b.observe(1000, True, "explorer.exe", False) is None
