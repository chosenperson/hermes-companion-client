"""Windows 截屏与前台窗口信息。

后端：
- ``mss``（默认）：GDI 复制桌面，兼容性最好；适合无边框窗口/窗口化游戏。
- ``dxcam``（可选，``pip install dxcam``）：DXGI Desktop Duplication，显卡侧复制，
  4K 下 CPU 更省；画面没变化时返回 None，本程序会沿用上一帧。

独占全屏游戏用两种后端都可能黑屏，建议游戏里改无边框窗口。
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

from PIL import Image

IS_WINDOWS = sys.platform == "win32"


@dataclass
class Foreground:
    title: str = ""
    process: str = ""
    rect: tuple[int, int, int, int] | None = None  # left, top, right, bottom（物理像素）
    window_class: str = ""


def is_shell_surface(fg: Foreground) -> bool:
    """Notification overflow/taskbar surfaces are not the requested app window."""
    return fg.window_class.casefold() in {
        "shell_traywnd", "shell_secondarytraywnd", "notifyiconoverflowwindow",
        "toplevelwindowforoverflowxamlisland", "controlcenterwindow",
    }


def enable_dpi_awareness() -> None:
    """让窗口坐标与截图都是物理像素，4K 缩放 150% 时不会错位。"""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def foreground() -> Foreground:
    if not IS_WINDOWS:
        return Foreground()
    import psutil
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return Foreground()
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        process = psutil.Process(pid.value).name()
    except Exception:
        process = ""
    rect = wintypes.RECT()
    ok = user32.GetWindowRect(hwnd, ctypes.byref(rect))
    box = (rect.left, rect.top, rect.right, rect.bottom) if ok else None
    window_class = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, window_class, len(window_class))
    return Foreground(title=buffer.value, process=process, rect=box, window_class=window_class.value)


LOCK_PROCESSES = {"lockapp.exe", "logonui.exe"}


def idle_seconds() -> float:
    """距最后一次键鼠输入的秒数（GetLastInputInfo）；非 Windows 返回 0。"""
    if not IS_WINDOWS:
        return 0.0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return float("inf")  # Unknown activity must not enable automatic capture.
    ctypes.windll.kernel32.GetTickCount.restype = ctypes.c_uint32
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def process_at(x: int, y: int) -> str:
    """屏幕坐标处顶层窗口的进程名（用于副屏隐私过滤）；非 Windows 返回空。"""
    if not IS_WINDOWS:
        return ""
    import psutil
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not hwnd:
        return ""
    root = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
    try:
        return psutil.Process(pid.value).name()
    except Exception:
        return ""


def is_locked(fg: "Foreground") -> bool:
    return not fg.process or fg.process.lower() in LOCK_PROCESSES


class Capturer:
    def __init__(self, backend: str = "mss", monitor: int = 1):
        self.backend = backend
        self.monitor = int(monitor)
        self._last: Image.Image | None = None
        if backend == "dxcam":
            import dxcam

            self._cam = dxcam.create(output_idx=self.monitor - 1, output_color="RGB")
            if self._cam is None:
                raise RuntimeError(f"dxcam 找不到显示器 {self.monitor}")
        elif backend == "mss":
            import mss

            self._mss = mss.mss()
            monitors = self._mss.monitors
            if not 1 <= self.monitor < len(monitors):
                raise RuntimeError(f"显示器编号 {self.monitor} 不存在，可用 1..{len(monitors) - 1}")
        else:
            raise ValueError(f"未知截图后端 {backend}")

    def close(self) -> None:
        if self.backend == "mss":
            self._mss.close()
        else:
            try:
                self._cam.release()
            except Exception:
                pass

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        """所选显示器在虚拟桌面里的 left, top, right, bottom。"""
        if self.backend == "mss":
            m = self._mss.monitors[self.monitor]
            return m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"]
        import mss  # dxcam 不直接给坐标，借 mss 查询一次

        with mss.mss() as probe:
            m = probe.monitors[self.monitor]
        return m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"]

    def grab(self) -> Image.Image | None:
        if self.backend == "mss":
            shot = self._mss.grab(self._mss.monitors[self.monitor])
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        frame = self._cam.grab()
        if frame is not None:
            self._last = Image.fromarray(frame)
        return self._last

    def window_on_monitor(self, fg: Foreground) -> bool:
        """前台窗口中心是否落在所选显示器上（只陪这块屏幕上的游戏）。"""
        if not fg.rect or fg.rect[2] <= fg.rect[0] or fg.rect[3] <= fg.rect[1]:
            return False
        left, top, right, bottom = self.bounds
        cx = (fg.rect[0] + fg.rect[2]) / 2
        cy = (fg.rect[1] + fg.rect[3]) / 2
        return left <= cx < right and top <= cy < bottom

    def covers_monitor(self, fg: Foreground, slack: int = 8) -> bool:
        """前台窗口是否铺满所选显示器（全屏或无边框窗口）。"""
        if not fg.rect or not self.window_on_monitor(fg):
            return False
        left, top, right, bottom = self.bounds
        return (fg.rect[0] <= left + slack and fg.rect[1] <= top + slack
                and fg.rect[2] >= right - slack and fg.rect[3] >= bottom - slack)

    def crop_to_window(self, image: Image.Image, fg: Foreground) -> Image.Image:
        """裁成前台窗口区域，同屏其它窗口不外传。"""
        if not fg.rect:
            raise ValueError("前台窗口没有有效区域，跳过截图")
        left, top, _, _ = self.bounds
        box = (max(0, fg.rect[0] - left), max(0, fg.rect[1] - top),
               min(image.width, fg.rect[2] - left), min(image.height, fg.rect[3] - top))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("前台窗口未与所选显示器相交，跳过截图")
        return image.crop(box)
