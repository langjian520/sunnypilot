# openpilot 完整 UI → 车机大屏（ui mirror）

把 comma 设备上 **openpilot 的完整界面**（车道线、置信球、路径、提示图标……不只是摄像头画面）
实时推到车机的安卓大屏上，用车机自带的浏览器就能看。

```
openpilot UI 渲染 ─► GL 纹理 ─►(抽帧回读)─► ffmpeg ─► MJPEG ─► 内置 HTTP 服务 ─► 车机浏览器
   60 fps           536x240     ~24 fps     mjpeg    本地 TCP      :8000         <img> 直接显示
                                                每帧独立 JPEG，端到端约 0.1 秒
```

抽帧倍率由 `MIRROR_FPS` 控制，默认 30（实际跑到 ~24，见「帧率」一节）。

设备上什么都不用装，**浏览器打开一个网址就行**：

```
http://<设备IP>:8000/
```

---

## 延迟

这是这套方案最花心思的地方。实测（comma four，同一 WiFi）：

| 指标 | HLS（旧版） | MJPEG（当前） |
|---|---|---|
| 首帧到达 | 2–3 秒 | **32 ms**（跨网络）/ 1 ms（本机） |
| 稳定播放前的等待 | 攒切片 + 播放器缓冲 | 无 |
| 帧间隔 | 1 秒一个切片 | 约 40 ms |
| 端到端延迟 | **约 3 秒** | **约 0.1 秒** |

差别来自协议本身，不是调参能救的：

- **HLS** 必须等 ffmpeg 攒满一个切片（1 秒）才发布，hls.js 还要再缓冲 2 个切片才敢播，
  光协议层就吃掉 3 秒。
- **MJPEG** 每帧都是一张独立 JPEG，编完立刻写出去，播放端收到就画 ——
  没有 GOP、没有切片、没有播放缓冲，延迟只剩「编码 + 传输 + 解码」。
  代价是每帧独立压缩、码率比 H.264 高（实测 1.4 Mbps，局域网毫无压力）。

所以默认走 MJPEG。**如果车机浏览器不认 MJPEG**（Safari/iOS 就不认），
网页会自动去查有没有 HLS 切片，有就切过去，没有就直接把 VLC 地址甩给你。

---

## 帧率

`MIRROR_FPS` 是**目标**帧率，不是保证值。抽帧是「每 N 帧取 1 帧」，
`N = round(60 / MIRROR_FPS)`，所以只有 60、30、20、15、12…… 这几档是整的：

| `MIRROR_FPS` | 抽帧倍率 | 实测帧率 | UI 进程 | ffmpeg | 合计 |
|---|---|---|---|---|---|
| 15 | 每 4 帧取 1 | 15.0 fps | 40 % | 20 % | 0.6 核 |
| 20 | 每 3 帧取 1 | 19.9 fps | 40 % | 30 % | 0.7 核 |
| **30（默认）** | 每 2 帧取 1 | **约 24 fps** | 50 % | 40 % | 0.9 核 |

（comma four，4 核，百分比是「单个核」的占用）

**注意 30 这一档达不到 30。** 回读要用 `glReadPixels` 把 GPU 画面拷回 CPU，
这个操作会阻塞渲染管线；每 2 帧拷一次时 UI 自己的渲染循环从 60fps 掉到约 48fps，
于是推流也跟着掉到 24fps 左右。偶尔还会有一两百毫秒的顿挫。
想要稳就退到 20 —— 那一档是干净的 19.9fps，UI 侧开销也更低。

**代价对比**：镜像关掉时 UI 约 10 %。所以打开 30 帧等于多花约 0.9 个核（4 核里的 22 %）。
行车时 modeld 等进程也在抢 CPU，实测可以，但发热和降频风险自己掂量。

---

## 为什么不能用常规投屏

comma 设备（comma three / four）的 AGNOS = Android 内核 + Ubuntu 用户空间，**没有
SurfaceFlinger，也没有 Wayland compositor**，画面是直接通过 DRM 画到屏幕上的。
所以 scrcpy、安卓投屏 App、车机互联盒子这类「从外部抓屏幕」的方案一律抓不到东西。

唯一可行的路子是**从设备内部的渲染循环里取帧**，也就是这个项目的做法。

---

## 工作原理

openpilot 自带一条录屏管道（`RECORD=1` 时启用）：每帧把渲染好的纹理回读成像素，喂给 ffmpeg。
本项目复用这条管道，但改成"抽帧 + 按需启停"，并且加了三层保护：

| 问题 | 做法 |
|---|---|
| 每帧回读 GPU 会拖垮 UI | 按目标帧率抽帧：`interval = target_fps / MIRROR_FPS`，只读 1/4 的帧 |
| 推流慢 / 没人看的时候会卡住渲染循环 | `put_nowait()` + `queue.Full` 直接丢帧，**绝不阻塞** |
| 不想一直占着 CPU | 每秒轮询一次开关，ffmpeg 的生死跟着开关走，不用重启 openpilot |

CPU 实测（comma four，4 核）：

| | UI 进程 | ffmpeg | 系统负载 |
|---|---|---|---|
| 开关关 | 10 % | — | 1.96 |
| 开关开 | 40 % | 20 % | 2.65 |

编 536×240@15fps 很便宜，主要开销在 GPU 回读那一步。MJPEG 比 H.264 费一点 CPU
（每帧独立压缩、没有帧间预测），但换来低一个数量级的延迟，值。

---

## 安装

```bash
# 在设备上（SSH 进去）
cd /data/ui-mirror-install/src
sudo bash install.sh
```

脚本会依次做六件事：

1. 装一份**完整版 ffmpeg** 到 `/data/ui-mirror/bin/`
   （comma 自带的 `/usr/local/venv/bin/ffmpeg` 是 openpilot 精简构建，
   只编了 `file,pipe` 协议，既没有 `mpjpeg` 也没有 `hls` 封装器，**发不出去**）
2. 装网页服务，注册为开机自启的 systemd 服务 `ui-mirror-web`
3. 写可调参数 `/data/ui-mirror/mirror.env`（已存在就不动，尊重你的改动）
4. 给 openpilot 打补丁
5. 装补丁自愈定时器 `ui-mirror-selfheal.timer`
6. 重启 openpilot 让补丁生效

装完浏览器打开 `http://<设备IP>:8000/` 即可。

---

## 使用

1. 设备上：**设置 → 设备 → ui mirror**，打开开关
2. 车机/手机/电脑浏览器打开 `http://<设备IP>:8000/`

打开约 1 秒出画面，关掉立刻停，**都不用重启 openpilot**。

> **车机要能连到这个 IP**：车机和 comma 得在同一个网络里（同一个 WiFi / 同一个手机热点）。
> 如果车机连的是车载 4G、comma 连的是手机热点，那是互相看不见的。
> 最省事的办法是让车机和 comma 都连你手机的热点，设备 IP 在 **设置 → WiFi** 里能看到
> （一般是 `192.168.x.x`）。

VLC 之类的播放器也可以直接打开 `http://<设备IP>:8000/stream.mjpeg`。

---

## 四个不明显的坑（都已在代码里处理）

### 1. 开关会被 openpilot 自己删掉

`common/params.cc` 的 `clearAll()`：

```cpp
auto it = keys.find(de->d_name);
if (it == keys.end() || (it->second.flags & key_flag)) unlink(...);
```

**"不在这张 native key 表里"的参数文件一律删掉。** 而 `system/manager/manager.py`
在启动时、以及每次 onroad / offroad / 点火切换时都会调它：

```python
params.clear_all(ParamKeyFlag.CLEAR_ON_MANAGER_START)
params.clear_all(ParamKeyFlag.CLEAR_ON_IGNITION_ON)   # ← 一上车就把开关清了
...
if ignition and not ignition_prev:
  params.clear_all(ParamKeyFlag.CLEAR_ON_IGNITION_ON)
```

设备是 prebuilt 构建（`launch_chffrplus.sh` 看到 `prebuilt` 标记就跳过 `build.py`），
`libparams_c.so` 里没有这个 key，也不想在设备上重编 native。

**做法**：在 `common/params.py` 里加一层影子存储 —— `UiMirrorEnabled` 每次被写入时
另存一份到 `/data/ui-mirror/params/`，`clear_all()` 之后再放回去。
（`SunnyconfPairingCode` 同样中招，一并修了。）

### 2. 屏幕一黑，镜像跟着黑

`application.py` 的渲染循环里有：

```python
# Skip rendering when screen is off
if not self._should_render:
  time.sleep(1 / self._target_fps)
  yield False, 0.0, 0.0
  continue
```

`_should_render` 由 `ui_state._set_awake()` 驱动（点火 or 无操作超时 or PC）。
停车、没点火时 30 秒后屏幕熄灭 → **一帧都不渲染** → 取帧代码在 `yield` 之后，永远走不到 →
ffmpeg 起不来。

**做法**：改成 `if not self._should_render and not ui_mirror_enabled():` ——
镜像开着就照常渲染，屏幕灭着也照样推流。

### 3. openpilot 更新会把补丁全部冲掉

`system/updated/updated.py` 的 `finalize_update()`：

```python
shutil.copytree(OVERLAY_MERGED, FINALIZED, symlinks=True)
run(["git", "reset", "--hard"], FINALIZED)          # ← 本地改动在这一步全没了
```

然后 `launch_chffrplus.sh` 把 finalized 整个替换掉 `/data/openpilot`。
过程**没有任何提示**，表现就是"过几天镜像突然不好使了"。

**做法**（两层）：

- `launch_chffrplus.sh` 本身有个设计好的信号：如果 `.git` 里有比 `.overlay_init` 新的文件，
  就跳过覆盖（这是 openpilot 留给"我在本地改代码"的口子）。安装脚本会
  `touch /data/openpilot/.git/.ui_mirror_devmode` 来利用它。
- 再加一个 **5 分钟一次的自愈定时器**：发现补丁没了就自动补回来，停车状态下还会
  自动重启 openpilot 让它生效（行驶中只补文件、绝不重启；重启有 10 分钟防抖）。

日志在 `/data/ui-mirror/selfheal.log`。

### 4. 屏幕关着的时候关开关，ffmpeg 会一直在后台烧 CPU

第 2 条那个渲染门是 `if not self._should_render and not ui_mirror_enabled()`。
反过来说：**屏幕关着 + 开关也关着**时，渲染循环会在 `yield` 之前就 `continue`，
于是排在 `yield` 之后的 `_update_ui_mirror()` 永远轮不到 ——
它才是唯一会去收 ffmpeg 的地方。

现场表现：停车（屏幕已熄）时在设置里关掉开关 → 画面确实没了，但
`pgrep ffmpeg` 还挂着一个进程，半个核一直空转，直到下次重启。

**做法**：在那个 `continue` 分支里补一次 `self._update_ui_mirror()`，
它会看到开关已关并把 ffmpeg 收干净。

---

## 调参

**别用 systemd drop-in**（这是踩过的坑）：`comma.service` 里跑的是
`tmux new-session -s comma -d /usr/comma/comma.sh`，而 **tmux server 是长驻的**，
新 session 继承的是 server 启动那一刻的环境。给 `comma.service` 加
`Environment=` 后重启，改进程里 `grep` 一下就知道 —— 环境变量根本没进去。

唯一每次启动都会重读的是 `launch_chffrplus.sh` 第 5 行的
`source "$DIR/launch_env.sh"`，所以补丁往 `launch_env.sh` 末尾挂了一行，
把下面这个文件 source 进来：

```bash
sudo nano /data/ui-mirror/mirror.env     # 改这里
sudo systemctl restart comma.service     # 改完重启生效
```

`/data/ui-mirror/` 不归 openpilot 更新管，所以参数不会被更新流程冲掉。
（万一 `mirror.env` 真的丢了，代码里的兜底值是保守的 `MIRROR_FPS=15`，不会失控。）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MIRROR` | 空 | `1` 强制常开并忽略设置里的开关；`0` 彻底关掉（连代码路径都不走） |
| `MIRROR_MODE` | `mjpeg` | `mjpeg` = 低延迟（默认）；`hls` = 兼容老浏览器，延迟 ~3 秒 |
| `MIRROR_FPS` | `30` | 推流**目标**帧率，实际能不能达到见「帧率」一节。UI 本身照旧 60fps |
| `MIRROR_QUALITY` | `6` | MJPEG 画质，数字越大越省流量/CPU（2 最好，31 最糊） |
| `MIRROR_SCALE` | `1.0` | 画面缩放。负载高就调到 `0.75` / `0.5` |
| `MIRROR_TCP_PORT` | `8554` | MJPEG 上游端口。改这里要同时改 `ui-mirror-web.service` 的 `UI_MIRROR_TCP` |
| `MIRROR_SEG_SEC` | `0.5` | 仅 `hls` 模式：切片长度 |
| `MIRROR_BITRATE` | `1200k` | 仅 `hls` 模式 |
| `MIRROR_ENCODER` | `libx264` | 仅 `hls` 模式，也可试 `h264_v4l2m2m`（硬编，会和 openpilot 抢编码器，慎用） |
| `MIRROR_FFMPEG` | `/data/ui-mirror/bin/ffmpeg` | 换 ffmpeg |
| `MIRROR_EXTRA_VF` | 空 | 方向不对时填 `transpose=1` / `hflip` 等（默认管道里已带 `vflip`） |

---

## 排错

```bash
systemctl status ui-mirror-web              # 网页服务
systemctl status ui-mirror-selfheal.timer   # 自愈定时器
cat /data/ui-mirror/selfheal.log            # 自愈日志

# 镜像没画面
cat /data/params/d/UiMirrorEnabled          # 开关是不是 1（不是就没有）
pgrep -af "/data/ui-mirror/bin/ffmpeg"      # ffmpeg 在跑吗
ss -ltn | grep 8554                         # MJPEG 上游端口在听吗
curl -s localhost:8000/health               # connected=True 才算链路通

# 顺手抓一帧看画面对不对（方向、内容）
/data/ui-mirror/bin/ffmpeg -y -i http://127.0.0.1:8000/stream.mjpeg -frames:v 1 /tmp/f.png
```

| 现象 | 原因 / 处理 |
|---|---|
| 网页提示"等待 openpilot 画面…" | 开关没开，或 ffmpeg 没起来。先看 `UiMirrorEnabled` 和 `/health` |
| 页面空白 / 一直转圈 | 车机和设备不在同一网段 —— 先在浏览器直接开 `http://<IP>:8000/health` 试试 |
| 画面方向不对 | 改 `MIRROR_EXTRA_VF`（默认管道里已带 `vflip`） |
| 提示"这个浏览器放不了" | 该浏览器不认 MJPEG（Safari），照提示用 VLC 打开 `stream.mjpeg` |
| 过几天突然不好使 | openpilot 更新冲掉了补丁 —— 看 `selfheal.log`，或重跑 `install.sh` |
| 设备发烫 / 卡顿 | 把 `MIRROR_FPS` 降到 20 或 15，或 `MIRROR_QUALITY=10` |
| 改了 `mirror.env` 没反应 | 没重启 openpilot，或那行忘了写 `export`。看 ffmpeg 命令行的 `-r` 确认 |
| 关掉开关后 ffmpeg 还在跑 | 老版本的 bug（屏幕关着时收不到停止信号）。重跑 `install.sh` |
| 画面偶尔顿一下 | 30 帧这一档在 comma four 上本来就到不了 30，退到 20 会稳很多 |

---

## 卸载

```bash
sudo systemctl disable --now ui-mirror-web ui-mirror-selfheal.timer
sudo rm -f /etc/systemd/system/ui-mirror-web.service \
           /etc/systemd/system/ui-mirror-selfheal.service \
           /etc/systemd/system/ui-mirror-selfheal.timer
rm -f /data/openpilot/.git/.ui_mirror_devmode

# 补丁回退（四个文件都有 .mirror.bak 备份）
cd /data/openpilot
cp openpilot/system/ui/lib/application.py.mirror.bak openpilot/system/ui/lib/application.py
cp openpilot/common/params.py.mirror.bak openpilot/common/params.py
cp openpilot/selfdrive/ui/mici/layouts/settings/device.py.mirror.bak openpilot/selfdrive/ui/mici/layouts/settings/device.py
cp launch_env.sh.mirror.bak launch_env.sh

sudo rm -rf /data/ui-mirror /data/ui-mirror-install
sudo systemctl restart comma.service
```

---

## 文件一览

| 路径 | 作用 |
|---|---|
| `install.sh` | 一键安装 / 重装 |
| `apply_mirror_patch.py` | 给 openpilot 打补丁（幂等，自动备份 `.mirror.bak`） |
| `selfheal.sh` | 补丁自愈（systemd timer 5 分钟跑一次） |
| `web_server.py` | 零依赖 HTTP 服务（标准库）：连 ffmpeg 的 MJPEG 流、拆帧转发；也发 HLS/网页 |
| `web/index.html` | 网页播放器（默认 MJPEG，8 秒没图就去看有没有 HLS，都没有就提示用 VLC） |
| `ui-mirror-web.service` | 网页服务的 systemd unit |
| `ui-mirror-selfheal.{service,timer}` | 自愈的 systemd unit |
| `mirror.env`（安装时生成） | 可调参数（帧率/画质/模式），落在 `/data/ui-mirror/`，不被 openpilot 更新覆盖 |

设备上的落点：程序和配置在 `/data/ui-mirror/`，systemd unit 在 `/etc/systemd/system/`。

补丁会动 openpilot 的**四个**文件，每个都留了 `.mirror.bak`：

| 文件 | 改了什么 |
|---|---|
| `openpilot/system/ui/lib/application.py` | 主体：抽帧回读 + ffmpeg 启停 + 渲染门 |
| `openpilot/common/params.py` | `UiMirrorEnabled` 放行 + 影子存储（扛 `clear_all()`） |
| `openpilot/selfdrive/ui/mici/layouts/settings/device.py` | 设置页里的开关 |
| `launch_env.sh` | 末尾挂一行，把 `/data/ui-mirror/mirror.env` source 进来 |

---

## 备注

- MJPEG 模式**不落盘**：帧在内存里直接转发，不写 eMMC，也没有需要清理的临时文件。
  切回 `MIRROR_MODE=hls` 时切片会写到 `/tmp/ui_mirror/`（tmpfs 内存盘），只保留 3 个。
- 走 MJPEG 而不是 RTSP/WebRTC，是为了**零依赖**：不用装 mediamtx，
  浏览器也不用 WebRTC 那套。本地 TCP + 标准库 HTTP 服务就够。
- 多个浏览器同时看是共享同一个上游连接的 —— 服务器只保留"最新一帧"，
  慢的客户端自动跳到最新画面，不会拖慢别人、也不会累积延迟。
- 这套改动是给 **comma four（mici UI）** 做的。comma three 的 UI 布局不同，
  设置页开关的位置要自己调（`apply_mirror_patch.py` 里那部分失败只会警告，不影响主功能）。
- 打补丁的顺序不能反：必须先补 `common/params.py` 的影子存储，
  否则参数活不过第一次 `clear_all()`。
- `mirror.env` 里每行都要写 `export`。`MIRROR_FPS=30` 这种光秃秃的赋值只是个
  shell 变量，不进 `environ`，python 那侧 `os.getenv` 读不到 —— 表现就是"改了没反应"。
  `launch_env.sh` 那侧加了 `set -a` 兜底，但别依赖它。
- 想确认改动真的生效，看 ffmpeg 的命令行最直接：

  ```bash
  tr '\0' ' ' < /proc/$(pgrep -x ffmpeg | head -1)/cmdline | grep -o '\-r [0-9]*'
  ```
