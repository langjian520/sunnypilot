# ui-mirror：把 openpilot UI 镜像到车机大屏

这个分支在官方 sunnypilot staging 的基础上，只多做了一件事：
给 UI 进程加了一条「把渲染好的画面编码推到网络」的输出。

comma four 的 AGNOS 是 Android 内核 + Ubuntu 用户空间，屏幕由 Linux 通过 DRM 直驱，
**没有 SurfaceFlinger、没有 weston**，所以 scrcpy / 安卓投屏 App / USB-C 转 HDMI 一律抓不到画面。
只能从 UI 进程内部取帧——本补丁就是干这个的。

## 这个分支改了什么

只有 1 个源文件 + 1 个工具目录：

| 文件 | 说明 |
| --- | --- |
| `openpilot/system/ui/lib/application.py` | UI 主程序，加了推流分支 + 丢帧保护（详见下方「实现要点」） |
| `openpilot/tools/ui-mirror/` | 服务端安装脚本、HLS 备用服务器、mediamtx 配置、补丁脚本 |

**其他文件和官方 staging 完全一致**，不影响纵向/横向控制、不影响你的 sunnyconf 自定义。

### 实现要点（两个不能省的保护）

openpilot 原本就有一条「每帧从 GPU 纹理取像素 → 喂给 ffmpeg」的录屏管道（`RECORD=1`），
补丁复用它，但补了两个对「持续推流」来说必须有的改造：

1. **抽帧**：60fps 全量回读 GPU 会把 UI 拖垮 → 按 `_target_fps / MIRROR_FPS` 抽帧（默认取 15fps）
2. **非阻塞丢帧**：原代码队列满时 `put()` 会**阻塞渲染循环**——车机没连或 Wi-Fi 变差就等于把 UI 卡死
   → 改成 `put_nowait`，队列满就丢帧。宁可掉画面，绝不影响 UI 与控制进程。

## 怎么用

补丁已经在这个分支里了，你只需要**在设备上装服务端并打开开关**：

```bash
# 1) SSH 到设备（设置 → Developer → enable SSH）
ssh comma@<设备IP>

# 2) 装服务端 + 注入环境变量（脚本会同时校验补丁是否还在）
cd /data/openpilot/openpilot/tools/ui-mirror
sudo bash install.sh              # 有 mediamtx 走 RTSP；拉不到就自动降级为 HLS
# sudo bash install.sh --hls      # 国内拉不动 GitHub 时，直接强制走 HLS
```

装好后车机上这样看：

| 方式 | 地址 | 延迟 |
| --- | --- | --- |
| RTSP（VLC / MX Player） | `rtsp://<设备IP>:8554/ui` | ~1 秒 |
| 浏览器 HLS（mediamtx） | `http://<设备IP>:8888/ui/index.m3u8` | 2~4 秒 |
| 浏览器 HLS（内置服务器） | `http://<设备IP>:8000/ui.m3u8` | 2~4 秒 |
| 浏览器 WebRTC（mediamtx） | `http://<设备IP>:8889/ui` | ~0.3 秒 |

> 服务端**没有开认证**，请让车机连 openpilot 自己的热点，别上公共 Wi-Fi。

## 环境变量

写在 `/etc/systemd/system/comma.service.d/10-ui-mirror.conf`，改完
`sudo systemctl restart comma.service` 生效。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MIRROR` | `1` | 总开关，改 `0` 即完全关闭（代码还在，只是不跑） |
| `MIRROR_URL` | `rtsp://127.0.0.1:8554/ui` | 支持 `rtsp://`、`hls://<目录>`、`udp://` |
| `MIRROR_FPS` | `15` | 推流帧率。UI 本身仍是 60fps，这里只决定取帧频率 |
| `MIRROR_SCALE` | `1.0` | 画面缩放。CPU 吃紧就降到 `0.75` |
| `MIRROR_BITRATE` | `1200k` | 糊/花屏提到 `2000k`；卡就降到 `800k` |
| `MIRROR_ENCODER` | `libx264` | 想试硬编改 `h264_v4l2m2m`（设备不一定支持，先 Device 面板看日志） |
| `MIRROR_EXTRA_VF` | 空 | **画面方向不对就改这里**：`transpose=1`（顺时针 90°）、`transpose=2`（逆时针 90°）、`transpose=3`、`hflip`、`vflip` |

## 让设备跑这个分支

更新器用 `git ls-remote --heads` 列可选分支，所以推到位后设备自己能发现。

- **如果设备的 origin 已经指向 `langjian520/sunnypilot`**：
  设备或网页 → 软件设置 → 目标分支里选 `staging-ui-mirror` 即可。
- **如果设备还指向官方 `sunnypilot/sunnypilot`**：先 SSH 到设备改远端
  ```bash
  cd /data/openpilot
  git remote -v                     # 确认当前 origin
  git remote set-url origin https://github.com/langjian520/sunnypilot.git
  ```
  然后到软件设置里选分支 `staging-ui-mirror`。

全新刷机也可以直接用 installer 指到这个仓库的这个分支。

## 后续同步官方更新

把上游 staging 合进来时，`openpilot/system/ui/lib/application.py` 大概率冲突或覆盖，
合完可能需要重新打一次补丁：

```bash
cd /data/openpilot
python3 openpilot/tools/ui-mirror/apply_mirror_patch.py \
        openpilot/system/ui/lib/application.py
```

脚本是幂等的（已打过会直接提示），每次改前自动备份 `<file>.mirror.bak`。
如果报「匹配到 0 处」，说明上游改了这段代码结构，需要手动照改。

## 卸载 / 恢复

```bash
# 只关推流：把 drop-in 里的 MIRROR 改成 0 后重启 comma.service
sudo systemctl restart comma.service

# 彻底还原（含服务端）
sudo bash openpilot/tools/ui-mirror/install.sh --uninstall
```

## 排错

| 现象 | 排查 |
| --- | --- |
| 车机连不上 | 设备与车机是否同网段；`systemctl status ui-mirror` 是否 active；部分车机热点开了「AP 隔离」 |
| 有画面但很卡 | 降 `MIRROR_FPS=10` / `MIRROR_SCALE=0.75` / `MIRROR_BITRATE=800k` |
| 画面花屏 | 提码率，或换播放器（HEVC/H.264 硬解支持差异很大） |
| 画面方向躺了 | 加 `MIRROR_EXTRA_VF=transpose=1`（或 2/3 试） |
| UI 变卡 | 立刻把 `MIRROR=0`，然后降帧率/缩放；正常情况不应感知到差异 |

## 免责

行车时盯着车机大屏看属于分心驾驶。**请只给副驾看或停车演示用。**
本补丁不改动任何控制逻辑，但任何第三方修改都请自行评估风险。
