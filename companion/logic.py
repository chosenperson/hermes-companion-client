"""与平台无关的决策逻辑：比帧、何时上传、隐私过滤、限速读取。可在任何系统上单测。"""

from __future__ import annotations

import io
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from PIL import Image, ImageChops, ImageStat

THUMB = (64, 36)


def thumbnail(image: Image.Image) -> Image.Image:
    """缩成 64x36 灰度图用于比帧，开销远小于一次截图。"""
    return image.convert("L").resize(THUMB, Image.BILINEAR)


def change_ratio(previous: Image.Image | None, current: Image.Image) -> float:
    """两张缩略图的平均像素差，0 = 完全相同，1 = 黑白反转。"""
    if previous is None:
        return 1.0
    diff = ImageChops.difference(previous, current)
    return round(ImageStat.Stat(diff).mean[0] / 255.0, 4)


def changed_fraction(previous: Image.Image | None, current: Image.Image, level: int = 12) -> float:
    """缩略图里明显变化（灰度差 ≥ level/255）的格子占比。

    平均差会把“小区域明显在动”（访谈/直播里的人像）稀释成接近 0，和暂停界面的转圈图标分不开；
    这个指标看的是有多大面积在动：转圈图标约 0.3%，人像约 2%，换场景 80%+。
    """
    if previous is None:
        return 1.0
    histogram = ImageChops.difference(previous, current).histogram()
    return round(sum(histogram[level:]) / (current.width * current.height), 4)


def encode_jpeg(image: Image.Image, max_width: int, quality: int) -> bytes:
    if image.width > max_width:
        height = round(image.height * max_width / image.width)
        image = image.resize((max_width, height), Image.LANCZOS)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=int(quality), optimize=True)
    return buffer.getvalue()


def process_allowed(process: str, allow: Iterable[str], deny: Iterable[str]) -> bool:
    """前台进程隐私过滤：命中黑名单不截；白名单非空时只截名单内进程。"""
    name = (process or "").lower()
    if any(name == d.lower() for d in deny):
        return False
    allow = [a.lower() for a in allow]
    return not allow or name in allow


@dataclass
class Decider:
    """决定这一轮是否上传及触发原因。时间都用本机 monotonic，服务器时间只用于取帧请求。"""

    last_upload: float = 0.0
    last_upload_server_ts: float = 0.0
    last_thumb: Any = None
    hotkey_pending: bool = False
    quiet_until: float = 0.0
    daily_quiet_until: float = 0.0
    jitter_unit: float = 0.0        # [-1, 1]，每次上传后重新随机，让定时间隔不那么规律
    history: list = field(default_factory=list)

    def _scaled(self, params: dict, key: str, default: float) -> float:
        spread = float(params.get("upload_jitter", 0))
        return float(params.get(key, default)) * (1 + self.jitter_unit * spread)

    def update_state(self, now: float, state: dict) -> None:
        """服务端告知接下来多少秒不会看自动上传的图（冷却/判断间隔/用户正在聊）。"""
        self.quiet_until = now + float(state.get("quiet_for_s") or 0)
        self.daily_quiet_until = now + float(state.get("daily_quiet_for_s") or 0)

    def decide(self, now: float, state: dict, change: float, scene: str | None = "game") -> str | None:
        """scene: game / daily；None 表示此刻不该自动上传（人不在、锁屏、日常关闭）。"""
        params = state.get("client") or {}
        if not state.get("enabled"):
            return None
        if self.hotkey_pending:
            return "hotkey"
        if float(state.get("want_frame_after") or 0) > self.last_upload_server_ts:
            return "request"
        if scene is None:
            return None
        if now < (self.daily_quiet_until if scene == "daily" else self.quiet_until):
            return None  # 服务端此时用不上，省流量
        since = now - self.last_upload if self.last_upload else float("inf")
        if scene == "daily":
            if change >= float(params.get("min_change", 0.12)) and since >= self._scaled(params, "daily_min_upload_s", 300):
                return "change"
            timer = self._scaled(params, "daily_timer_upload_s", 900)
            return "timer" if timer and since >= timer else None
        if state.get("mode") == "passive":
            interval = self._scaled(params, "passive_upload_s", 60)
            return "timer" if interval and since >= interval and change >= 0.01 else None
        if change >= float(params.get("min_change", 0.12)) and since >= float(params.get("min_upload_interval_s", 15)):
            return "change"
        timer = self._scaled(params, "timer_upload_s", 120)
        if timer and since >= timer:
            return "timer"
        return None

    def uploaded(self, now: float, thumb: Any, server_ts: float | None, trigger: str) -> None:
        self.last_upload = now
        self.last_thumb = thumb
        if server_ts:
            self.last_upload_server_ts = float(server_ts)
        if trigger == "hotkey":
            self.hotkey_pending = False
        self.jitter_unit = random.uniform(-1, 1)
        self.history = (self.history + [(now, trigger)])[-50:]


class ThrottledReader(io.RawIOBase):
    """按 kbps 限速的只读流，requests 逐块读取上传，避免网游延迟尖峰。"""

    def __init__(self, data: bytes, kbps: float, chunk: int = 16 * 1024, clock=time.monotonic, sleep=time.sleep):
        self._data = memoryview(data)
        self._pos = 0
        self._rate = max(float(kbps), 64.0) * 1000 / 8  # bytes/s
        self._chunk = chunk
        self._start = None
        self._clock = clock
        self._sleep = sleep

    def readable(self) -> bool:
        return True

    def __len__(self) -> int:
        return len(self._data)

    def tell(self) -> int:
        """Expose the offset so requests does not add chunked framing."""
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        if self._start is None:
            self._start = self._clock()
        size = self._chunk if size is None or size < 0 else min(size, self._chunk)
        piece = bytes(self._data[self._pos:self._pos + size])
        self._pos += len(piece)
        ahead = self._pos / self._rate - (self._clock() - self._start)
        if ahead > 0:
            self._sleep(ahead)
        return piece


# 铺满屏幕也不当作游戏的常见程序（浏览器、桌面、编辑器、办公、播放器、系统界面）
NON_GAME_PROCESSES = [
    "explorer.exe", "msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe",
    "code.exe", "cursor.exe", "devenv.exe", "idea64.exe", "pycharm64.exe", "windowsterminal.exe", "cmd.exe",
    "powershell.exe", "pwsh.exe", "notepad.exe", "notepad++.exe", "winword.exe", "excel.exe", "powerpnt.exe",
    "acrobat.exe", "obsidian.exe", "siyuan.exe", "potplayermini64.exe", "potplayer.exe", "vlc.exe", "mpv.exe",
    "applicationframehost.exe", "searchhost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "lockapp.exe", "textinputhost.exe", "systemsettings.exe", "taskmgr.exe", "steamwebhelper.exe",
    "steam.exe", "epicgameslauncher.exe", "wegame.exe",
]


def looks_like_game(process: str, covers_monitor: bool, allow: Iterable[str], deny: Iterable[str],
                    non_game: Iterable[str]) -> bool:
    """白名单非空时只认名单；否则前台窗口铺满陪玩屏且不是常见非游戏程序就当作在玩游戏。"""
    name = (process or "").lower()
    if not name or not process_allowed(name, allow, deny):
        return False
    if any(True for _ in allow):
        return True
    return covers_monitor and name not in {n.lower() for n in non_game}


@dataclass
class AutoSession:
    """进游戏自动开陪玩，离开游戏一段时间自动关；只关自己开的那一次，尊重用户手动结束。"""

    stop_after_s: float = 600
    active: bool = False            # 当前陪玩是否由自动开启
    confirmed: bool = False         # 自动开启后服务端已确认开启（区分“开启失败”与“用户手动结束”）
    suppressed: str = ""            # 用户手动结束时正在玩的游戏：退出它之前不再自动开
    last_game: float = 0.0
    last_game_process: str = ""

    def observe(self, now: float, enabled: bool, process: str, scene: str | None) -> str | None:
        """scene: game=在玩游戏；daily=日常模式下人在电脑前；None=都不是（离开/锁屏/非目标）。"""
        name = (process or "").lower() if scene == "game" else "*"
        is_game = scene is not None  # 下面沿用“目标出现”的语义：游戏或日常都算
        if is_game:
            self.last_game = now
            self.last_game_process = name
        if self.active and enabled:
            self.confirmed = True
        if self.active and not enabled and not self.confirmed:
            return "start" if is_game else None  # 上次开启没成功（如断网），继续尝试
        if self.active and not enabled:
            # 飞书或托盘已手动结束
            self.active = False
            self.suppressed = self.last_game_process if now - self.last_game < 30 else ""
            return None
        if self.suppressed:
            # 手动结束后：人离开足够久，或开始一款新游戏，才恢复自动
            left = not is_game and now - self.last_game >= self.stop_after_s
            new_game = scene == "game" and name != self.suppressed
            if left or new_game:
                self.suppressed = ""
        if is_game and not enabled and not self.suppressed:
            self.active, self.confirmed = True, False
            return "start"
        if self.active and enabled and not is_game and now - self.last_game >= self.stop_after_s:
            self.active = False
            return "stop"
        return None

    def manual(self, now: float, action: str) -> None:
        if action == "stop":
            self.active = False
            self.suppressed = self.last_game_process if now - self.last_game < 30 else ""
        elif action == "start":
            self.active = False  # 手动开的不自动关
