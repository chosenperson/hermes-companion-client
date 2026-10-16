"""与平台无关的决策逻辑：比帧、何时上传、隐私过滤、限速读取。可在任何系统上单测。"""

from __future__ import annotations

import io
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
    history: list = field(default_factory=list)

    def update_state(self, now: float, state: dict) -> None:
        """服务端告知接下来多少秒不会看自动上传的图（冷却/判断间隔/用户正在聊）。"""
        self.quiet_until = now + float(state.get("quiet_for_s") or 0)

    def decide(self, now: float, state: dict, change: float) -> str | None:
        params = state.get("client") or {}
        if not state.get("enabled"):
            return None
        if self.hotkey_pending:
            return "hotkey"
        if float(state.get("want_frame_after") or 0) > self.last_upload_server_ts:
            return "request"
        if now < self.quiet_until:
            return None  # 服务端此时用不上，省流量
        since = now - self.last_upload if self.last_upload else float("inf")
        if state.get("mode") == "passive":
            interval = float(params.get("passive_upload_s", 60))
            return "timer" if interval and since >= interval and change >= 0.01 else None
        if change >= float(params.get("min_change", 0.12)) and since >= float(params.get("min_upload_interval_s", 15)):
            return "change"
        timer = float(params.get("timer_upload_s", 120))
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
