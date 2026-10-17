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
import atexit
import random
import ctypes
from datetime import datetime
import faulthandler
import hashlib
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import requests

from . import __version__
from .capture import Capturer, Foreground, enable_dpi_awareness, foreground, idle_seconds, is_locked, process_at, is_shell_surface
from .logic import (NON_GAME_PROCESSES, AutoSession, Decider, ThrottledReader, change_ratio, changed_fraction,
                    encode_jpeg,
                    looks_like_game, process_allowed, thumbnail)

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
    "auto_session": True,
    "auto_stop_after_s": 600,
    "non_game_processes": NON_GAME_PROCESSES,
    "watch_monitor": None,           # 副屏编号；填了才有“陪看副屏”
    "hotkey_watch": "ctrl+alt+w",
    "hotkey_look": "ctrl+alt+s",
    "hotkey_pause": "ctrl+alt+p",
    "proxy": "",
    "last_upload_dir": "",  # Opt-in local evidence: exact last accepted payload and metadata.
}
FALLBACK_STATE = {"enabled": False, "mode": "auto", "want_frame_after": 0, "client": {}}


def app_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()


def acquire_instance_lock() -> bool:
    """Keep manual launch and login startup from creating duplicate clients."""
    if sys.platform != "win32":
        return True
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    name = hashlib.sha256(str(app_dir().resolve()).casefold().encode()).hexdigest()[:24]
    handle = kernel.CreateMutexW(None, False, "Local\\HermesCompanion-" + name)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    if error == 183:  # ERROR_ALREADY_EXISTS
        kernel.CloseHandle(handle)
        return False
    atexit.register(kernel.CloseHandle, handle)
    return True


def load_config(path: str | None, *, require_auth: bool = True) -> dict:
    file = Path(path) if path else app_dir() / "config.json"
    config = dict(DEFAULT_CONFIG)
    if file.is_file():
        config.update(json.loads(file.read_text(encoding="utf-8")))
    config["token"] = os.environ.get("COMPANION_TOKEN") or config["token"]
    config["server_url"] = os.environ.get("COMPANION_SERVER_URL") or config["server_url"]
    if require_auth and (not config["server_url"] or not config["token"]):
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
        proxy = config.get("proxy")
        if proxy:
            self.session.trust_env = False
            if proxy != "direct":
                self.session.proxies = {"http": proxy, "https": proxy}

    def state(self) -> dict:
        resp = self.session.get(f"{self.base}/state", timeout=15)
        resp.raise_for_status()
        return resp.json()

    def control(self, action: str) -> dict:
        resp = self.session.post(f"{self.base}/control", json={"action": action}, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def upload(self, jpeg: bytes, trigger: str, change: float, title: str, kbps: float,
               scene: str | None = "game", source: str | None = None) -> dict:
        headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(jpeg)),
            "X-Companion-Trigger": trigger,
            "X-Companion-Change": f"{change:.4f}",
            "X-Companion-Title": urllib.parse.quote(title[:120]),
            "X-Companion-Scene": scene or "daily",
        }
        if source:
            headers["X-Companion-Source"] = source
        resp = self.session.post(f"{self.base}/frame", data=ThrottledReader(jpeg, kbps),
                                 headers=headers, timeout=60)
        resp.raise_for_status()
        return resp.json()


class Companion:
    def __init__(self, config: dict):
        self.config = config
        self.api = Api(config)
        self.capturer: Capturer | None = None
        self._last_auto = 0.0
        self.scene: str | None = None
        self.watch_capturer: Capturer | None = None
        self.watch_check_s = 60.0  # 副屏比帧间隔：用来判断是否还在播
        self.watch = {"thumb": None, "last_check": 0.0, "last_change": 0.0, "last_upload": 0.0,
                      "server_ts": 0.0, "since": 0.0}  # 在截图线程里创建：Windows GDI 句柄不能跨线程用
        self.decider = Decider()
        self.state = dict(FALLBACK_STATE)
        self.paused = False
        self.status = "启动中"
        self.stats = {"uploads": 0, "bytes": 0, "skipped_private": 0}
        self.auto = AutoSession(stop_after_s=float(config["auto_stop_after_s"])) if config["auto_session"] else None
        self.heartbeat = time.monotonic()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.on_change = lambda: None

    # ---- 外部操作（托盘/热键）
    def look_now(self) -> None:
        log.info("收到看一眼请求")
        self.decider.hotkey_pending = True
        self._wake.set()

    def toggle_watch(self) -> None:
        if not self.config.get("watch_monitor"):
            self._set_status("未配置副屏（config.json 的 watch_monitor）")
            return
        self.control("watch_off" if self.state.get("watch") else "watch_on")

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        log.info("本机暂停切换：%s", self.paused)
        self._set_status("本机已暂停截图" if self.paused else "已恢复")

    def _accept_state(self, state: dict) -> None:
        if bool(state.get("watch")) != bool(self.state.get("watch")):
            self.watch["since"] = 0.0
        self.state = state
        self.decider.update_state(time.monotonic(), state)

    def control(self, action: str, source: str = "manual") -> None:
        log.info("控制请求：%s source=%s", action, source)
        if self.auto and source == "manual":
            self.auto.manual(time.monotonic(), action)
        try:
            self._accept_state(self.api.control(action))
            self._set_status(self._describe())
        except Exception as exc:
            self._set_status(f"操作失败：{type(exc).__name__}")
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ---- 主循环
    def _set_status(self, text: str) -> None:
        if text == self.status:
            return
        log.info(text)
        self.status = text
        try:
            self.on_change()
        except Exception:  # 托盘刷新失败不能拖垮截图线程
            log.exception("托盘刷新失败")

    def _describe(self) -> str:
        if not self.state.get("enabled"):
            return "陪玩未开启"
        if self.paused:
            return "本机已暂停截图"
        mode = "主动" if self.state.get("mode") == "auto" else "只在问时看"
        extra = ("，含日常" if self.state.get("daily") else "") + ("，陪看副屏" if self.state.get("watch") else "")
        return f"陪玩中（{mode}{extra}）"

    def _poll_state(self) -> None:
        try:
            self._accept_state(self.api.state())
            self._set_status(self._describe())
        except Exception as exc:
            self._set_status(f"连不上服务器：{type(exc).__name__}")

    def run(self) -> None:
        try:
            self._loop()
        except BaseException:
            log.exception("截图线程异常退出")
            raise

    def _loop(self) -> None:
        last_poll = 0.0
        while not self._stop.is_set():
            self.heartbeat = time.monotonic()
            params = self.state.get("client") or {}
            now = time.monotonic()
            enabled = bool(self.state.get("enabled"))
            poll_every = float(params.get("poll_s", 5) if enabled else params.get("idle_poll_s", 30))
            if now - last_poll >= poll_every or (self.decider.hotkey_pending and not enabled):
                self._poll_state()
                last_poll = now
            if self.state.get("watch") and not self.paused and self.config.get("watch_monitor"):
                try:
                    self._watch_once()
                except Exception as exc:
                    log.exception("副屏陪看失败")
                    self._set_status(f"副屏陪看失败：{type(exc).__name__}: {exc}")
                    self._reset_watch_capturer()
            elif not self.state.get("watch"):
                self.watch["since"] = 0.0
                self._reset_watch_capturer()
            if self.state.get("enabled") and not self.paused:
                try:
                    self._capture_once()
                except Exception as exc:
                    log.exception("截图/上传失败")
                    self._set_status(f"截图/上传失败：{type(exc).__name__}: {exc}")
                    self._reset_capturer()
            elif self.decider.hotkey_pending and self.paused:
                self.decider.hotkey_pending = False
                self._set_status("本机已暂停截图，热键忽略")
            elif self.decider.hotkey_pending:
                log.info("热键开启陪玩")
                self.control("start")  # 保留 hotkey_pending，下一轮带 hotkey 上传
                continue
            if self.auto and not self.paused and time.monotonic() - self._last_auto >= 3:
                self._last_auto = time.monotonic()
                try:
                    self._auto_check()
                except Exception:
                    log.exception("自动陪玩检测失败")
            interval = float(params.get("capture_interval_s", 3)) if self.state.get("enabled") else 1.0
            self._wake.wait(interval)
            self._wake.clear()

    def _ensure_capturer(self) -> Capturer:
        if self.capturer is None:
            self.capturer = Capturer(self.config["backend"], self.config["monitor"])
            log.info("截图后端 %s 显示器 %s 已就绪", self.config["backend"], self.config["monitor"])
        return self.capturer

    def _scene(self, fg) -> str | None:
        """game=在玩游戏；daily=日常模式下人在电脑前；None=离开、锁屏、隐私窗口或日常关闭。"""
        cfg = self.config
        params = self.state.get("client") or {}
        if is_locked(fg) or is_shell_surface(fg) or idle_seconds() >= float(params.get("daily_idle_s", 300)):
            return None
        if not process_allowed(fg.process, cfg["allow_processes"], cfg["deny_processes"]):
            return None
        capturer = self._ensure_capturer()
        if not capturer.window_on_monitor(fg):
            return None
        covers = capturer.covers_monitor(fg)
        if looks_like_game(fg.process, covers, cfg["allow_processes"], cfg["deny_processes"],
                           cfg["non_game_processes"]):
            return "game"
        if self.state.get("daily") and not cfg["allow_processes"] and self.capturer.window_on_monitor(fg):
            return "daily"
        return None

    def _auto_check(self) -> None:
        fg = foreground()
        scene = self._scene(fg)
        if scene is None and self.state.get("watch"):
            scene = "watch"  # 陪看副屏时不碰键鼠也算在场，不自动结束
        action = self.auto.observe(time.monotonic(), bool(self.state.get("enabled")), fg.process, scene)
        if action:
            log.info("自动%s陪玩（%s，%s）", "开启" if action == "start" else "结束", scene or "离开", fg.process or "-")
            self.control(action, source="auto")

    def _watch_once(self) -> None:
        """副屏陪看：每分钟比一次帧判断是否还在播，按 watch_upload_s 上传；静止或超时自动结束。"""
        params = self.state.get("client") or {}
        now = time.monotonic()
        w = self.watch
        if not w["since"]:
            w.update(since=now, last_change=now, last_check=0.0, thumb=None,
                     last_upload=0.0, server_ts=0.0, blocked_reason="")
        if now - w["since"] >= float(params.get("watch_max_s", 10800)):
            log.info("陪看达到最长时长，自动结束")
            return self.control("watch_off", source="auto")
        requested = float(self.state.get("want_frame_after") or 0) > w["server_ts"]
        due = not w["last_upload"] or now - w["last_upload"] >= float(w.get("next_interval") or 0)
        if now - w["last_check"] < self.watch_check_s and not requested and not due:
            return
        if is_locked(foreground()):
            return  # Lock screen must block watch uploads as well as primary uploads.
        if self.watch_capturer is None:
            self.watch_capturer = Capturer(self.config["backend"], self.config["watch_monitor"])
            log.info("副屏 %s 已就绪", self.config["watch_monitor"])
        left, top, right, bottom = self.watch_capturer.bounds
        center_process = process_at((left + right) // 2, (top + bottom) // 2)
        if not center_process or not process_allowed(center_process, [], self.config["deny_processes"]):
            if w.get("blocked_reason") != (center_process or "unknown"):
                log.info("副屏中心窗口不允许截图：%s", center_process or "unknown")
                w["blocked_reason"] = center_process or "unknown"
            w["last_check"] = now
            return  # 副屏中间是聊天等隐私窗口
        w["blocked_reason"] = ""
        image = self.watch_capturer.grab()
        if image is None:
            return
        if (is_locked(foreground())
                or process_at((left + right) // 2, (top + bottom) // 2).lower() != center_process.lower()):
            self._reset_watch_capturer()
            return  # Discard a frame taken while the center window was changing.
        thumb = thumbnail(image)
        change = change_ratio(w["thumb"], thumb)
        moving = changed_fraction(w["thumb"], thumb)
        w.update(last_check=now, thumb=thumb)
        # 暂停界面常有转圈/光标等小动效：按“明显在动的面积”判断是否还在播，而不是要求 100% 相同
        if moving >= float(params.get("watch_motion_fraction", 0.01)) or change >= 0.02:
            w["last_change"] = now
        if now - w["last_change"] >= float(params.get("watch_static_stop_s", 900)):
            log.info("副屏画面长时间不动，自动结束陪看")
            return self.control("watch_off", source="auto")
        if not (requested or due):
            return
        jpeg = encode_jpeg(image, int(params.get("max_width", 1600)), int(params.get("jpeg_quality", 70)))
        result = self.api.upload(jpeg, "request" if requested else "timer", change, center_process,
                                 float(params.get("upload_kbps", 1500)), "watch",
                                 source=f"{self.config['source']}-side")
        self._accept_state(result.get("state") or self.state)
        if result.get("accepted") is not True:
            log.warning("接收端未接受副屏截图，不计成功上传")
            return
        w["last_upload"] = now
        spread = float(params.get("upload_jitter", 0))
        w["next_interval"] = float(params.get("watch_upload_s", 180)) * (1 + random.uniform(-spread, spread))
        w["server_ts"] = float(result.get("frame_ts") or w["server_ts"])
        self.stats["uploads"] += 1
        self.stats["bytes"] += len(jpeg)
        trigger = "request" if requested else "timer"
        self._save_last_upload(jpeg, Foreground(process=center_process, rect=(left, top, right, bottom)),
                               "watch", trigger, result.get("frame_ts"), monitor=self.config["watch_monitor"])
        log.info("上传 watch/%s %.0fKB 变化%.2f %s bytes=%d", trigger,
                 len(jpeg) / 1024, change, center_process, len(jpeg))

    def _reset_watch_capturer(self) -> None:
        try:
            if self.watch_capturer is not None:
                self.watch_capturer.close()
        except Exception:
            log.exception("关闭副屏截图后端失败")
        self.watch_capturer = None

    def _reset_capturer(self) -> None:
        try:
            if self.capturer is not None:
                self.capturer.close()
        except Exception:
            pass
        self.capturer = None

    def _capture_once(self) -> None:
        cfg = self.config
        fg = foreground()
        if is_locked(fg) or is_shell_surface(fg) or not process_allowed(fg.process, cfg["allow_processes"], cfg["deny_processes"]):
            self.stats["skipped_private"] += 1
            return
        self._ensure_capturer()
        if not self.capturer.window_on_monitor(fg):
            return  # 前台窗口不在陪玩这块屏上
        scene = self._scene(fg)
        if scene is None and not self.decider.hotkey_pending and \
                float(self.state.get("want_frame_after") or 0) <= self.decider.last_upload_server_ts:
            return  # 人不在/锁屏/日常关闭：不截图
        image = self.capturer.grab()
        if image is None:
            return
        if cfg["capture_area"] == "window":
            image = self.capturer.crop_to_window(image, fg)
        latest = foreground()
        if (latest.process.lower() != fg.process.lower() or latest.rect != fg.rect
                or latest.title != fg.title or is_locked(latest)):
            return  # Foreground changed during capture: discard rather than upload another window.
        thumb = thumbnail(image)
        change = change_ratio(self.decider.last_thumb, thumb)
        trigger = self.decider.decide(time.monotonic(), self.state, change, scene)
        if not trigger:
            return
        params = self.state.get("client") or {}
        jpeg = encode_jpeg(image, int(params.get("max_width", 1600)), int(params.get("jpeg_quality", 70)))
        result = self.api.upload(jpeg, trigger, change, fg.title, float(params.get("upload_kbps", 1500)), scene)
        self._accept_state(result.get("state") or self.state)
        if result.get("accepted") is not True:
            log.warning("接收端未接受截图，不计成功上传")
            return
        self.decider.uploaded(time.monotonic(), thumb, result.get("frame_ts"), trigger)
        self.stats["uploads"] += 1
        self.stats["bytes"] += len(jpeg)
        self._save_last_upload(jpeg, fg, scene, trigger, result.get("frame_ts"))
        log.info("上传 %s/%s %.0fKB 变化%.2f %s bytes=%d", scene or "-", trigger,
                 len(jpeg) / 1024, change, fg.process, len(jpeg))
        self._set_status(self._describe())

    def _save_last_upload(self, jpeg: bytes, fg, scene, trigger, frame_ts, *, monitor=None) -> None:
        directory = self.config.get("last_upload_dir")
        if not directory:
            return
        try:
            root = Path(directory)
            if scene == "watch":
                root = root / "watch"
            root.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(jpeg).hexdigest()
            image_name = f"last-upload-{digest}.jpg"
            image = root / image_name
            temporary = root / "last-upload.tmp"
            temporary.write_bytes(jpeg)
            temporary.replace(image)
            metadata = {
                "saved_at": datetime.now().astimezone().isoformat(), "frame_ts": frame_ts,
                "client_version": __version__, "image": image_name, "sha256": digest,
                "bytes": len(jpeg), "process": fg.process, "title": fg.title,
                "rect": fg.rect, "window_class": fg.window_class,
                "monitor": self.config["monitor"] if monitor is None else monitor,
                "scene": scene or "daily", "trigger": trigger,
            }
            temporary = root / "last-upload.json.tmp"
            temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(root / "last-upload.json")
            for previous in root.glob("last-upload-*.jpg"):
                if previous.name != image_name:
                    previous.unlink()
            log.info("已留存最后成功上传图 sha256=%s bytes=%d", digest, len(jpeg))
        except OSError:
            log.exception("最后上传图本地留存失败（远端上传已成功）")


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

    icon_class = pystray.Icon
    if sys.platform == "win32":
        class WindowsIcon(pystray.Icon):
            def _on_notify(self, wparam, lparam):
                if lparam == 0x0205:  # WM_RBUTTONUP, before TrackPopupMenuEx opens the menu.
                    log.info("托盘菜单打开")
                    self.update_menu()
                try:
                    return super()._on_notify(wparam, lparam)
                finally:
                    if lparam == 0x0205:
                        log.info("托盘菜单关闭")

        icon_class = WindowsIcon

    def color() -> str:
        if not app.state.get("enabled"):
            return "#888888"
        return "#d9a400" if app.paused else "#2e9d5b"

    def item(text, action, **kw):
        def invoke(icon, descriptor):
            log.info("托盘操作：%s", descriptor.text)
            return action()
        return pystray.MenuItem(text, invoke, **kw)

    menu = pystray.Menu(
        pystray.MenuItem(lambda _i: app.status, None, enabled=False),
        item("开始陪玩", lambda: app.control("start")),
        item("结束陪玩", lambda: app.control("stop")),
        item("主动搭话", lambda: app.control("auto")),
        item("只在问时看", lambda: app.control("passive")),
        pystray.MenuItem(lambda _i: "日常也陪：开（点击关闭）" if app.state.get("daily") else "日常也陪：关（点击开启）",
                         lambda icon, _item: app.control("daily_off" if app.state.get("daily") else "daily_on")),
        pystray.MenuItem(lambda _i: (f"陪看副屏：开（{app.config['hotkey_watch']} 关闭）" if app.state.get("watch")
                                     else f"陪看副屏：关（{app.config['hotkey_watch']} 开启）"),
                         lambda icon, _item: app.toggle_watch(), visible=bool(app.config.get("watch_monitor"))),
        pystray.Menu.SEPARATOR,
        item(f"让伙伴看一眼（{app.config['hotkey_look']}）", app.look_now),
        item(lambda _i: "恢复截图" if app.paused else f"暂停截图（{app.config['hotkey_pause']}）", app.toggle_pause),
        pystray.Menu.SEPARATOR,
        item("退出", lambda: (app.stop(), icon.stop())),
    )
    icon = icon_class("hermes-companion", _icon(color()), "Hermes 陪玩", menu)

    def refresh():
        icon.icon = _icon(color())
        title = f"Hermes 陪玩：{app.status}"
        icon.title = title.encode("utf-16-le")[:254].decode("utf-16-le", errors="ignore")
        if sys.platform != "win32":
            icon.update_menu()

    app.on_change = refresh
    icon.run()


def bind_hotkeys(app: Companion) -> None:
    try:
        import keyboard

        keyboard.add_hotkey(app.config["hotkey_look"], app.look_now)
        keyboard.add_hotkey(app.config["hotkey_pause"], app.toggle_pause)
        if app.config.get("watch_monitor"):
            keyboard.add_hotkey(app.config["hotkey_watch"], app.toggle_watch)
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


def setup_logging() -> None:
    """控制台 + 程序目录下 companion.log（1MB×3 轮转）；托盘模式没有控制台也能事后排查。"""
    fmt = logging.Formatter("%(asctime)s %(threadName)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
    try:
        handler = logging.handlers.RotatingFileHandler(app_dir() / "companion.log", maxBytes=1_000_000,
                                                       backupCount=3, encoding="utf-8")
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError:
        pass


def start_watchdog(app: "Companion", stall_s: float = 60) -> None:
    """截图线程超过 stall_s 没有心跳时，把所有线程调用栈写进 companion.log（每次卡住只写一次）。"""
    path = app_dir() / "companion-stall.log"

    def watch() -> None:
        dumped = False
        while not app._stop.wait(10):
            stalled = time.monotonic() - app.heartbeat > stall_s
            if stalled and not dumped:
                log.error("截图线程 %.0f 秒没有心跳，调用栈已写入 %s", time.monotonic() - app.heartbeat, path.name)
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(f"\n==== {time.strftime('%F %T')} stall ====\n")
                    faulthandler.dump_traceback(file=handle, all_threads=True)
            dumped = stalled

    threading.Thread(target=watch, name="watchdog", daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(prog="hermes-companion")
    parser.add_argument("--config")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--bench", type=int, metavar="N")
    parser.add_argument("--once", metavar="FILE")
    parser.add_argument("--list-monitors", action="store_true")
    args = parser.parse_args()
    setup_logging()
    enable_dpi_awareness()

    if args.list_monitors:
        import mss

        for i, m in enumerate(mss.mss().monitors[1:], 1):
            print(f"{i}: {m['width']}x{m['height']} @ ({m['left']},{m['top']})")
        return
    config = load_config(args.config, require_auth=not (args.bench or args.once))
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

    if not acquire_instance_lock():
        log.info("客户端已经运行，跳过重复启动")
        return
    app = Companion(config)
    log.info("Hermes 陪玩客户端 %s 启动，来源 %s", __version__, config["source"])
    bind_hotkeys(app)
    worker = threading.Thread(target=app.run, name="capture", daemon=True)
    worker.start()
    start_watchdog(app)
    if args.no_tray:
        try:
            while worker.is_alive():
                worker.join(1)
        except KeyboardInterrupt:
            app.stop()
    else:
        run_tray(app)
        app.stop()
