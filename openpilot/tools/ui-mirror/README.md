# ui-mirror：把 openpilot UI 镜像到车机大屏

这个分支在官方 sunnypilot staging 的基础上只多做一件事：给 UI 进程加了一条
「把渲染好的画面编码推到网络」的输出，并在**设置 → 设备 → ui mirror** 里放了一个开关。

comma four 的 AGNOS 是 Android 内核 + Ubuntu 用户空间，屏幕由 Linux 通过 DRM 直驱，
**没有 SurfaceFlinger、没有 weston**，所以 scrcpy / 安卓投屏 App / USB-C 转 HDMI 一律抓不到画面。
只能从 UI 进程内部取帧——本补丁就是干这个的。

## 怎么用

### 1）装服务端（一次性）

```bash
ssh comma@<设备IP>
cd /data/openpilot/openpilot/tools/ui-mirror
sudo bash install.sh              # 有 mediamtx 走 RTSP；拉不到就自动降级为 HLS
# sudo bash install.sh --hls      # 国内拉不动 GitHub 时，直接强制走 HLS
```

### 2）开开关

设备上：**设置 → 设备 → ui mirror**（中文界面显示「画面镜像到车机」）。

或者命令行：

```bash
python3 -c "from openpilot.common.params import Params; Params().put_bool('UiMirrorEnabled', True)"
python3 -c "from openpilot.common.params import Params; Params().put_bool('UiMirrorEnabled', False)"   # 关
```

开关**不需要重启 openpilot**：打开后 1 秒内 ffmpeg 自动起来，关掉自动收掉。

### 3）车机上看

| 方式 | 地址 | 延迟 |
| --- | --- | --- |
| RTSP（VLC / MX Player） | `rtsp://<设备IP>:8554/ui` | ~1 秒 |
| 浏览器 HLS（mediamtx） | `http://<设备IP>:8888/ui/index.m3u8` | 2~4 秒 |
| 浏览器 HLS（内置服务器） | `http://<设备IP>:8000/ui.m3u8` | 2~4 秒 |
| 浏览器 WebRTC（mediamtx） | `http://<设备IP>:8889/ui` | ~0.3 秒 |

> 服务端**没有开认证**，请让车机连 openpilot 自己的热点，别上公共 Wi-Fi。

## 这个分支改了什么

| 文件 | 说明 |
| --- | --- |
| `openpilot/system/ui/lib/application.py` | UI 主程序：推流分支 + 抽帧 + 丢帧保护 + 按需启停 |
| `openpilot/selfdrive/ui/mici/layouts/settings/device.py` | 设备设置页里多一个 `ui mirror` 开关 |
| `openpilot/common/params_keys.h` | 新增参数 `UiMirrorEnabled`（BOOL，默认 0，会备份） |
| `openpilot/common/params.py` | 让预编译设备上也能读写这个参数（见下方说明） |
| `openpilot/selfdrive/ui/translations/app_zh-CHS.po` | 开关的中文文案 |
| `openpilot/tools/ui-mirror/` | 服务端安装脚本、HLS 备用服务器、mediamtx 配置、补丁脚本 |

**其他文件和官方 staging 完全一致**，不影响纵向/横向控制，不影响你的 sunnyconf 自定义。

### 为什么开关能实时生效

UI 每一渲染周期只要花一次 `getattr` 判断，真正的参数读取**每秒最多一次**（缓存在
`ui_mirror_enabled()` 里），所以不会拖累渲染。ffmpeg 的启停跟着开关走：

- 开 → 起 ffmpeg（libx264 ultrafast/zerolatency）→ 按 15fps 抽帧喂过去
- 关 → 置哨兵让写线程退出 → 关管道 → `terminate()` → 下一轮 tick 回收进程

也就是说**关着的时候完全不占 CPU**，不用为此重启 openpilot，也不用担心忘关。

### 两个不能省的保护

openpilot 原本就有一条「每帧从 GPU 纹理取像素 → 喂给 ffmpeg」的录屏管道（`RECORD=1`），
补丁复用它，但补了两个对「持续推流」来说必须有的改造：

1. **抽帧**：60fps 全量回读 GPU 会把 UI 拖垮 → 按 `_target_fps / MIRROR_FPS` 取（默认 15fps）
2. **非阻塞丢帧**：原代码队列满时 `put()` 会**阻塞渲染循环**——车机没连或 Wi-Fi 变差就等于把 UI 卡死
   → 改成 `put_nowait`，队列满就丢帧。宁可掉画面，绝不影响 UI 与控制进程。

### 关于 params key 的坑

新增的参数在 `params_keys.h` 里登记了，但**预编译设备上的 `libparams_c.so` 是在这个 key
出现之前编好的**（`launch_chffrplus.sh` 因为有 `prebuilt` 标记会跳过 `./build.py`）。
原生层 put/get bool 其实都是纯文件读写、不看这张表，所以这里沿用 sunnyconf 的做法：
在 `params.py` 的 `check_key()` 里给这类 key 放行（见 `_KEYS_UNKNOWN_TO_PREBUILT_LIBPARAMS`），
省得每次更新还得在设备上重编 native 代码。

## 调参（环境变量）

写在 `/etc/systemd/system/comma.service.d/10-ui-mirror.conf`，改完
`sudo systemctl restart comma.service` 生效。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MIRROR` | 不设置 | `1`=绕开开关常开、`0`=彻底关掉（连代码路径都不走）。**一般不用动** |
| `MIRROR_URL` | `rtsp://127.0.0.1:8554/ui` | 支持 `rtsp://`、`hls://<目录>`、`udp://` |
| `MIRROR_FPS` | `15` | 推流帧率。UI 本身仍是 60fps，这里只决定取帧频率 |
| `MIRROR_SCALE` | `1.0` | 画面缩放。CPU 吃紧就降到 `0.75` |
| `MIRROR_BITRATE` | `1200k` | 糊/花屏提到 `2000k`；卡就降到 `800k` |
| `MIRROR_ENCODER` | `libx264` | 想试硬编改 `h264_v4l2m2m`（设备不一定支持） |
| `MIRROR_EXTRA_VF` | 空 | **画面方向不对就改这里**：`transpose=1`（顺时针 90°）、`transpose=2`（逆时针 90°）、`transpose=3`、`hflip`、`vflip` |

## 后续同步官方更新

把上游 staging 合进来时，`openpilot/system/ui/lib/application.py` 大概率冲突或被覆盖，
合完可能需要重新打一次补丁：

```bash
cd /data/openpilot
python3 openpilot/tools/ui-mirror/apply_mirror_patch.py \
        openpilot/system/ui/lib/application.py
```

脚本是幂等的（已打过会直接提示），每次改前自动备份 `<file>.mirror.bak`。
另外别忘了把设置页那几行（`device.py`）和 params 的改动一并带过来。

## 卸载 / 恢复

```bash
# 只关推流：设置里关掉开关即可，或
python3 -c "from openpilot.common.params import Params; Params().put_bool('UiMirrorEnabled', False)"

# 彻底还原（含服务端）
cd /data/openpilot/openpilot/tools/ui-mirror && sudo bash install.sh --uninstall
```

## 排错

| 现象 | 排查 |
| --- | --- |
| 设页里找不到 ui mirror 开关 | 确认跑的是本分支；设备设置页 → 设备，滚一屏 |
| 开了开关没画面 | `systemctl status ui-mirror` 是否 active；`journalctl -u comma -n 50` 看 ffmpeg 报什么 |
| 车机连不上 | 设备与车机是否同网段；部分车机热点开了「AP 隔离」 |
| 有画面但很卡 | 降 `MIRROR_FPS=10` / `MIRROR_SCALE=0.75` / `MIRROR_BITRATE=800k` |
| 画面花屏 | 提码率，或换播放器（硬解支持差异很大） |
| 画面方向躺了 | 加 `MIRROR_EXTRA_VF=transpose=1`（或 2/3 试） |
| UI 变卡 | 立刻关掉开关，然后降帧率/缩放；正常情况不应感知到差异 |

## 免责

行车时盯着车机大屏看属于分心驾驶。**请只给副驾看或停车演示用。**
本补丁不改动任何控制逻辑，但任何第三方修改都请自行评估风险。
