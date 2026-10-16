"""陪玩客户端主程序：托盘 + 热键 + 截图循环。

用法：
  python -m companion                 正常运行（托盘）
  python -m companion --no-tray       无托盘，日志输出到控制台
  python -m companion --check         检查服务器连通与令牌
  python -m companion --bench 20      本机截图开销测试，不上传
  python -m companion --once shot.jpg 截一张按上传规格保存到本地，不上传
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import requests

from . import __version__
from .capture import Capturer, enable_dpi_awareness, foreground
from .logic import Decider, ThrottledReader, change_ratio, encode_jpeg, process_allowed, thumbnail

log = logging.getLogger("companion")

DEFAULT_CONFIG = {
    "server_url": "",
    "token": "",
    "source": "pc/monitor1",
    "monitor": 1,
    "backend": "mss",
    "capture_area": "window",
    "allow_processes": [],
    "deny_processes": [
        "WeChat.exe", "Weixin.exe", "QQ.exe", "Feishu.exe", "Lark.exe", "Telegram.exe", "Discord.exe",
        "DingTalk.exe", "Outlook.exe", "olk.exe", "KeePass.exe", "KeePassXC.exe", "1Password.exe",
        "Bitwarden.exe", "mstsc.exe",
    ],
    "hotkey_look": "ctrl+alt+s",
    "hotkey_pause": "ctrl+alt+p",
    "proxy": "",
}
FALLBACK_STATE = {"enabled": False, "mode": "auto", "want_frame_after": 0, "client": {}}


def app_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()


def load_config(path: str | None) -> dict:
    file = Path(path) if path else app_dir() / "config.json"
    config = dict(DEFAULT_CONFIG)
    if file.is_file():
        config.update(json.loads(file.read_text(encoding="utf-8")))
    config["token"] = os.environ.get("COMPANION_TOKEN") or config["token"]
    config["server_url"] = os.environ.get("COMPANION_SERVER_URL") or config["server_url"]
    if not config["server_url"] or not config["token"]:
        raise SystemExit(f"请在 {file} 里填写 server_url 和 token（参考 config.example.json）")
    config["server_url"] = config["server_url"].rstrip("/")
    return config


class Api:
    def __init__(self, config: dict):
        self.base = config["server_url"]
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {config['token']}",
            "X-Companion-Source": config["source"],
            "X-Companion-Client": f"win/{__version__}",
        })
        if config.get("proxy"):
            self.session.proxies = {"http": config["proxy"], "https": config["proxy"]}

    def state(self) -> dict:
        resp = self.session.get(f"{self.base}/state", timeout=15)
        resp.raise_for_status()
        return resp.json()

    def control(self, action: str) -> dict:
        resp = self.session.post(f"{self.base}/control", json={"action": action}, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def upload(self, jpeg: bytes, trigger: str, change: float, title: str, kbps: float) -> dict:
        headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(jpeg)),
            "X-Companion-Trigger": trigger,
            "X-Companion-Change": f"{change:.4f}",
            "X-Companion-Title": urllib.parse.quote(title[:120]),
        }
        resp = self.session.post(f"{self.base}/frame", data=ThrottledReader(jpeg, kbps),
                                 headers=headers, timeout=60)
        resp.raise_for_status()
        return resp.json()


class Companion:
    def __init__(self, config: dict):
        self.config = config
        self.api = Api(config)
        self.capturer = Capturer(config["backend"], config["monitor"])
        self.decider = Decider()
        self.state = dict(FALLBACK_STATE)
        self.paused = False
        self.status = "启动中"
        self.stats = {"uploads": 0, "bytes": 0, "skipped_private": 0}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.on_change = lambda: None

    # ---- 外部操作（托盘/热键）
    def look_now(self) -> None:
        self.decider.hotkey_pending = True
        self._wake.set()

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self._set_status("本机已暂停截图" if self.paused else "已恢复")

    def control(self, action: str) -> None:
        try:
            self.state = self.api.control(action)
            self._set_status(self._describe())
        except Exception as exc:
            self._set_status(f"操作失败：{type(exc).__name__}")
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ---- 主循环
    def _set_status(self, text: str) -> None:
        if text != self.status:
            log.info(text)
        self.status = text
        self.on_change()

    def _describe(self) -> str:
        if not self.state.get("enabled"):
            return "陪玩未开启"
        if self.paused:
            return "本机已暂停截图"
        return "陪玩中（主动）" if self.state.get("mode") == "auto" else "陪玩中（只在问时看）"

    def _poll_state(self) -> None:
        try:
            self.state = self.api.state()
            self._set_status(self._describe())
        except Exception as exc:
            self._set_status(f"连不上服务器：{type(exc).__name__}")

    def run(self) -> None:
        last_poll = 0.0
        while not self._stop.is_set():
            params = self.state.get("client") or {}
            now = time.monotonic()
            enabled = bool(self.state.get("enabled"))
            poll_every = float(params.get("poll_s", 5) if enabled else params.get("idle_poll_s", 30))
            if now - last_poll >= poll_every or (self.decider.hotkey_pending and not enabled):
                self._poll_state()
                last_poll = now
            if self.state.get("enabled") and not self.paused:
                try:
                    self._capture_once()
                except Exception as exc:
                    self._set_status(f"截图/上传失败：{type(exc).__name__}: {exc}")
            elif self.decider.hotkey_pending:
                self.decider.hotkey_pending = False
                self._set_status("陪玩未开启或已暂停，热键忽略")
            interval = float(params.get("capture_interval_s", 3)) if self.state.get("enabled") else 1.0
            self._wake.wait(interval)
            self._wake.clear()

    def _capture_once(self) -> None:
        cfg = self.config
        fg = foreground()
        if not process_allowed(fg.process, cfg["allow_processes"], cfg["deny_processes"]):
            self.stats["skipped_private"] += 1
            return
        if not self.capturer.window_on_monitor(fg):
            return  # 前台窗口不在陪玩这块屏上
        image = self.capturer.grab()
        if image is None:
            return
        if cfg["capture_area"] == "window":
            image = self.capturer.crop_to_window(image, fg)
        thumb = thumbnail(image)
        change = change_ratio(self.decider.last_thumb, thumb)
        trigger = self.decider.decide(time.monotonic(), self.state, change)
        if not trigger:
            return
        params = self.state.get("client") or {}
        jpeg = encode_jpeg(image, int(params.get("max_width", 1600)), int(params.get("jpeg_quality", 70)))
        result = self.api.upload(jpeg, trigger, change, fg.title, float(params.get("upload_kbps", 1500)))
        self.state = result.get("state") or self.state
        self.decider.uploaded(time.monotonic(), thumb, result.get("frame_ts"), trigger)
        self.stats["uploads"] += 1
        self.stats["bytes"] += len(jpeg)
        log.info("上传 %s %.0fKB 变化%.2f %s", trigger, len(jpeg) / 1024, change, fg.process)
        self._set_status(self._describe())


# ---------------------------------------------------------------- 托盘与热键

def _icon(color: str):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((6, 6, 58, 58), fill=color)
    draw.ellipse((20, 22, 28, 30), fill="white")
    draw.ellipse((36, 22, 44, 30), fill="white")
    return image


def run_tray(app: Companion) -> None:
    import pystray

    def color() -> str:
        if not app.state.get("enabled"):
            return "#888888"
        return "#d9a400" if app.paused else "#2e9d5b"

    def item(text, action, **kw):
        return pystray.MenuItem(text, lambda icon, _item: action(), **kw)

    menu = pystray.Menu(
        pystray.MenuItem(lambda _i: app.status, None, enabled=False),
        item("开始陪玩", lambda: app.control("start")),
        item("结束陪玩", lambda: app.control("stop")),
        item("主动搭话", lambda: app.control("auto")),
        item("只在问时看", lambda: app.control("passive")),
        pystray.Menu.SEPARATOR,
        item(f"让伙伴看一眼（{app.config['hotkey_look']}）", app.look_now),
        item(lambda _i: "恢复截图" if app.paused else f"暂停截图（{app.config['hotkey_pause']}）", app.toggle_pause),
        pystray.Menu.SEPARATOR,
        item("退出", lambda: (app.stop(), icon.stop())),
    )
    icon = pystray.Icon("hermes-companion", _icon(color()), "Hermes 陪玩", menu)

    def refresh():
        icon.icon = _icon(color())
        icon.title = f"Hermes 陪玩：{app.status}"
        icon.update_menu()

    app.on_change = refresh
    icon.run()


def bind_hotkeys(app: Companion) -> None:
    try:
        import keyboard

        keyboard.add_hotkey(app.config["hotkey_look"], app.look_now)
        keyboard.add_hotkey(app.config["hotkey_pause"], app.toggle_pause)
    except Exception as exc:  # 没装 keyboard 或权限不足时不影响托盘
        log.warning("热键未启用：%s", exc)


# ---------------------------------------------------------------- 诊断命令

def bench(config: dict, count: int) -> None:
    import psutil

    cap = Capturer(config["backend"], config["monitor"])
    proc = psutil.Process()
    proc.cpu_percent(None)
    timings, sizes = [], []
    started = time.perf_counter()
    for _ in range(count):
        t0 = time.perf_counter()
        image = cap.grab()
        if image is None:
            continue
        thumbnail(image)
        t1 = time.perf_counter()
        sizes.append(len(encode_jpeg(image, 1600, 70)))
        timings.append(((t1 - t0) * 1000, (time.perf_counter() - t1) * 1000))
        time.sleep(0.2)
    wall = time.perf_counter() - started
    cpu = proc.cpu_percent(None)
    if not timings:
        print("没有截到画面")
        return
    grab = sorted(t[0] for t in timings)
    enc = sorted(t[1] for t in timings)
    print(f"后端 {config['backend']} 显示器 {config['monitor']} 分辨率 {image.width}x{image.height}")
    print(f"截图+比帧 中位 {grab[len(grab)//2]:.1f}ms 最大 {grab[-1]:.1f}ms（每 3 秒一次）")
    print(f"压缩上传图 中位 {enc[len(enc)//2]:.1f}ms（只在触发上传时）")
    print(f"上传图平均 {sum(sizes)/len(sizes)/1024:.0f}KB；测试期间进程 CPU {cpu:.1f}%（{wall:.1f}s 内连续截 {len(timings)} 张）")


def main() -> None:
    parser = argparse.ArgumentParser(prog="hermes-companion")
    parser.add_argument("--config")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--bench", type=int, metavar="N")
    parser.add_argument("--once", metavar="FILE")
    parser.add_argument("--list-monitors", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    enable_dpi_awareness()

    if args.list_monitors:
        import mss

        for i, m in enumerate(mss.mss().monitors[1:], 1):
            print(f"{i}: {m['width']}x{m['height']} @ ({m['left']},{m['top']})")
        return
    config = load_config(args.config)
    if args.bench:
        return bench(config, args.bench)
    if args.once:
        image = Capturer(config["backend"], config["monitor"]).grab()
        Path(args.once).write_bytes(encode_jpeg(image, 1600, 70))
        print(f"已保存 {args.once}（未上传）")
        return
    if args.check:
        state = Api(config).state()
        print(json.dumps({k: state.get(k) for k in ("enabled", "mode")}, ensure_ascii=False))
        print("连通正常，令牌有效")
        return

    app = Companion(config)
    bind_hotkeys(app)
    worker = threading.Thread(target=app.run, name="capture", daemon=True)
    worker.start()
    if args.no_tray:
        try:
            while worker.is_alive():
                worker.join(1)
        except KeyboardInterrupt:
            app.stop()
    else:
        run_tray(app)
        app.stop()
