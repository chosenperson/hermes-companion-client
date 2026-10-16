# Hermes 陪玩客户端（Windows）

让 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 角色“坐在旁边看你玩”：
本程序在电脑上低频截取游戏画面，**只在画面有明显变化、你按热键或对方想看时**才上传一张压缩图；
服务端判断值得开口时，角色会在你的聊天软件（如放在旁边的手机/平板上的飞书）里搭话。
你在手机上问“你看这个”“这关怎么过”，角色也会让电脑立刻传一张最新画面再回答。

本仓库只有电脑端。服务端（Hermes 插件 + 截图接收端）不在这里，接口见文末。

## 工作方式

```
每 3 秒截一次所选显示器 → 缩成 64x36 灰度图和上次上传比
  ├─ 变化够大且距上次上传 ≥15 秒 → 上传（change）
  ├─ 两分钟都没变化 → 兜底传一张（timer）
  ├─ 服务端请求新画面（你在手机上问了）→ 上传（request）
  ├─ 按热键 → 上传并让角色立刻说话（hotkey）
  └─ 服务端冷却/正在聊天期间（state.quiet_for_s）只响应热键和取帧请求，自动上传暂停
上传前：裁成前台窗口 → 缩到 1600 宽 → JPEG 70 → 限速 1.5Mbps
```

采集参数（间隔、阈值、宽度、限速）由服务端下发，改服务端配置即可生效，不用重装客户端。

## 性能与带宽

- 截图在每 3 秒一次的低频下进行，比帧只处理 64x36 的缩略图；压缩只在要上传时发生。
- 首次部署请用 `--bench` 在游戏机上实测（见下文），同时观察游戏帧数和 1% Low。
- 4K 显示器上 `mss`（GDI）后端每次复制约几十毫秒 CPU；在意的话安装 `dxcam` 改用 DXGI 后端。
- 单张上传约 100–300KB；按默认节流，平均约每分钟 0–4 张，每小时 0–30MB。
- 上传限速默认 1.5Mbps，避免联网游戏瞬时延迟尖峰；可在服务端调 `upload_kbps`。

## 隐私

- 陪玩**未开启时不截图**；开启/结束在托盘菜单或直接跟角色说。
- 只截所选显示器，且前台窗口必须在这块屏幕上；默认只裁前台窗口区域，同屏其它窗口不外传。
- 前台是聊天、邮件、密码管理器、远程桌面等进程时不截（`deny_processes`，可增删）。
  需要更严格时填 `allow_processes`，只截名单里的游戏。
- `Ctrl+Alt+P` 随时暂停本机截图；结束陪玩时服务端会删除已收截图。
- 服务端看图模型会把聊天/文档等非游戏画面标为隐私，不描述其内容。

## 安装

需要 Python 3.10+（Windows 10/11）。

```powershell
git clone https://github.com/chosenperson/hermes-companion-client
cd hermes-companion-client
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy config.example.json config.json   # 然后填写 server_url、token、monitor
.venv\Scripts\python -m companion --list-monitors
.venv\Scripts\python -m companion --check
.venv\Scripts\python -m companion --bench 20
.venv\Scripts\python -m companion
```

打包成单个 exe（可选）：

```powershell
.venv\Scripts\pip install pyinstaller
.venv\Scripts\pyinstaller --onefile --noconsole --name hermes-companion companion\__main__.py
```

`config.json` 放在 exe 同目录。开机自启：把 exe 快捷方式放进 `shell:startup`。

## 配置 `config.json`

| 字段 | 说明 |
|---|---|
| `server_url` | 接收端地址，形如 `https://<域名>/companion/v1`；也可用环境变量 `COMPANION_SERVER_URL` |
| `token` | 上传令牌，也可用环境变量 `COMPANION_TOKEN`；不要提交到仓库 |
| `source` | 本机/屏幕标识，如 `pc-c/4k` |
| `monitor` | 显示器编号（`--list-monitors` 查看），选打游戏的那块 |
| `backend` | `mss`（默认）或 `dxcam` |
| `capture_area` | `window`（只截前台窗口，默认）或 `monitor`（整块屏） |
| `allow_processes` / `deny_processes` | 进程白名单 / 黑名单（如 `eldenring.exe`） |
| `hotkey_look` / `hotkey_pause` | 默认 `ctrl+alt+s` 让角色看一眼，`ctrl+alt+p` 暂停截图 |
| `proxy` | 留空走系统代理；也可填 `http://127.0.0.1:7890` |

独占全屏游戏可能截出黑屏，请在游戏设置里改“无边框窗口”。HDR 开启时截图颜色可能偏灰，不影响识别。

## 托盘图标

灰 = 陪玩未开启，绿 = 陪玩中，黄 = 本机暂停。菜单：开始/结束陪玩、主动搭话、只在问时看、让伙伴看一眼、暂停截图、退出。

## 服务端接口（供自建）

均需 `Authorization: Bearer <token>`（health 除外）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/companion/v1/health` | 存活检查 |
| GET | `/companion/v1/state` | `{enabled, mode, want_frame_after, quiet_for_s, server_time, client:{采集参数}}` |
| POST | `/companion/v1/frame` | body 为 JPEG/PNG/WebP；头 `X-Companion-Source/Trigger/Change/Title`（Title 为 URL 编码）；返回 `{accepted, frame_ts, state}` |
| POST | `/companion/v1/control` | `{"action": "start|stop|auto|passive"}` |

## 开发

```bash
pip install Pillow requests psutil pytest
python -m pytest -q tests
```

逻辑（比帧、上传决策、限速、隐私过滤）都在 `companion/logic.py`，不依赖 Windows，可在任意系统测试。

## License

MIT
