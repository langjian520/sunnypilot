#!/usr/bin/env python3
"""
给 openpilot 的 UI 打补丁，让渲染好的完整界面（车道线 / 置信球 / 提示图标）
实时推到网络上，而不是只在 1.9 寸屏上显示。

原理：复用 openpilot 自带的 RECORD 录屏管道（每帧从渲染纹理取像素 -> 管道喂 ffmpeg），
改成"抽帧 + 推流 + 按需启停"，并且取帧失败/推流慢时丢弃帧而不是卡住 UI。

开关：设置 → 设备 → "ui mirror"（底层是 params 的 UiMirrorEnabled，见 README）。
      打开后 1 秒内 ffmpeg 自动起来，关掉自动收掉，不用重启 openpilot。

一共补三个文件（后两个是现场踩出来的坑，见对应注释）：
  openpilot/system/ui/lib/application.py              取帧 / 启停 / 开关
  openpilot/common/params.py                          UiMirrorEnabled 活过 clear_all()
  openpilot/selfdrive/ui/mici/layouts/settings/device.py  设置页里的开关

用法（在 comma 设备上、以 root 运行）：
  python3 apply_mirror_patch.py                     # 打补丁（默认路径）
  python3 apply_mirror_patch.py /path/to/application.py
  python3 apply_mirror_patch.py --uninstall         # 从备份恢复

幂等：已打过补丁会直接提示退出；每次修改前自动备份为 <file>.mirror.bak

注意：AGNOS 的更新机制会在「有暂存好的更新」落地时整个替换 /data/openpilot，
      本地改动会被冲掉。install.sh 会装一个 systemd timer 定期自检并补回补丁。

调参用这些环境变量（写在 comma.service 的 drop-in 里）：
  MIRROR=1                                    强制常开（忽略设置里的开关）
  MIRROR=0                                    彻底关掉（连代码路径都不走）
  MIRROR_FFMPEG=/data/ui-mirror/bin/ffmpeg    用哪个 ffmpeg。
        comma 自带的 /usr/local/venv/bin/ffmpeg 是 openpilot 精简构建，只认
        file/pipe 两种协议、没有 hls 封装器，所以必须用完整版（install.sh 会装）。
  MIRROR_MODE=mjpeg                           输出方式：
                 mjpeg = 每帧独立 JPEG，走本地 TCP 给 web_server，端到端 ~0.3 秒（默认）
                 hls   = 切成切片，端到端 ~3 秒，好处是 VLC / Safari 都能直接放
  MIRROR_TCP_PORT=8554                        mjpeg 模式：ffmpeg 监听的本地端口
  MIRROR_QUALITY=6                            mjpeg 画质，2 最好 / 31 最省流量
  MIRROR_SEG_SEC=0.5                          hls 模式：切片长度，越短延迟越低
  MIRROR_FPS=15                               推流帧率，UI 本身照旧 60fps
  MIRROR_SCALE=1.0                            画面缩放，压力大就调小
  MIRROR_BITRATE=1200k                        仅 hls 模式使用
  MIRROR_ENCODER=libx264                      仅 hls 模式使用，或 h264_v4l2m2m（硬编，会和 openpilot 抢编码器，慎用）
  MIRROR_EXTRA_VF=                            画面方向不对时填 transpose=1 / hflip 等
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_TARGET = "/data/openpilot/openpilot/system/ui/lib/application.py"
# UI 装在 /data/openpilot 下，仓库根目录
DEFAULT_BASEDIR = "/data/openpilot"
MARKER = "# --- ui mirror (added by apply_mirror_patch.py) ---"

# ---------------------------------------------------------------- 补丁片段

ENV_BLOCK = '''OFFSCREEN = os.getenv("OFFSCREEN") == "1"  # Disable FPS limiting for fast offline rendering
'''

ENV_NEW = ENV_BLOCK + '''
# --- ui mirror (added by apply_mirror_patch.py) ---
MIRROR_ENV = os.getenv("MIRROR", "")                # "1"=强制开、"0"=彻底关、不写=听设置里的开关
MIRROR_AVAILABLE = MIRROR_ENV != "0"                # 是否具备推流能力
MIRROR_FFMPEG = os.getenv("MIRROR_FFMPEG", "/data/ui-mirror/bin/ffmpeg")  # 必须是完整版 ffmpeg
MIRROR_URL = os.getenv("MIRROR_URL", "hls:///tmp/ui_mirror/live.m3u8")
MIRROR_FPS = int(os.getenv("MIRROR_FPS", "15"))     # 推流帧率，UI 本身仍是 60fps
MIRROR_SCALE = float(os.getenv("MIRROR_SCALE", "1.0"))  # 画面缩放，负载高就调小
MIRROR_BITRATE = os.getenv("MIRROR_BITRATE", "1200k")
MIRROR_ENCODER = os.getenv("MIRROR_ENCODER", "libx264")  # 或 h264_v4l2m2m（硬编，看设备是否支持）
MIRROR_EXTRA_VF = os.getenv("MIRROR_EXTRA_VF", "")       # 画面方向不对时: transpose=1 或 hflip 等
MIRROR_MODE = os.getenv("MIRROR_MODE", "mjpeg")          # mjpeg=低延迟(默认) / hls=兼容老浏览器
MIRROR_TCP_PORT = int(os.getenv("MIRROR_TCP_PORT", "8554"))  # mjpeg 模式：ffmpeg 监听的本地端口
MIRROR_QUALITY = int(os.getenv("MIRROR_QUALITY", "6"))   # mjpeg 画质：2 最好 / 31 最省流量
MIRROR_SEG_SEC = float(os.getenv("MIRROR_SEG_SEC", "0.5"))  # hls 模式切片长度，越短延迟越低

_ui_mirror_state = {"on": False, "checked": -1.0, "params": None}


def ui_mirror_enabled() -> bool:
  """当前要不要把 UI 镜像出去。

  MIRROR=1 强制开、MIRROR=0 彻底关，否则看设置里的开关（params: UiMirrorEnabled）。
  param 每秒最多读一次，免得每帧都去打扰 paramsd。
  """
  if MIRROR_ENV == "1":
    return True
  if MIRROR_ENV == "0":
    return False

  now = time.monotonic()
  if now - _ui_mirror_state["checked"] < 1.0:
    return _ui_mirror_state["on"]
  _ui_mirror_state["checked"] = now

  try:
    if _ui_mirror_state["params"] is None:
      from openpilot.common.params import Params
      _ui_mirror_state["params"] = Params()
    _ui_mirror_state["on"] = bool(_ui_mirror_state["params"].get_bool("UiMirrorEnabled"))
  except Exception:
    # params 还没起来、或者用的是没有这个 key 的旧 libparams —— 一律当关
    _ui_mirror_state["on"] = False
  return _ui_mirror_state["on"]
# --- end ui mirror ---
'''

TRY_TEXTURE_OLD = "      needs_render_texture = self._scale != 1.0 or BURN_IN_MODE or RECORD\n"
TRY_TEXTURE_NEW = "      needs_render_texture = self._scale != 1.0 or BURN_IN_MODE or RECORD or MIRROR_AVAILABLE\n"

METHODS_ANCHOR = "  @contextmanager\n  def _startup_profile_context(self):\n"

METHODS_NEW = '''  def _start_ui_mirror(self):
    """按需起 ffmpeg：只有设置里的开关打开时才跑，关掉就整个收掉，不占 CPU。"""
    scale_filter = "" if MIRROR_SCALE == 1.0 else f",scale=iw*{MIRROR_SCALE}:ih*{MIRROR_SCALE}"
    extra_filter = "" if not MIRROR_EXTRA_VF else f",{MIRROR_EXTRA_VF}"
    vf = f'vflip{scale_filter}{extra_filter}'

    ffmpeg_args = [
      MIRROR_FFMPEG,
      '-v', 'error',
      '-nostats',
      '-f', 'rawvideo',
      '-pix_fmt', 'rgba',
      '-s', f'{self._scaled_width}x{self._scaled_height}',
      '-r', str(MIRROR_FPS),
      '-i', 'pipe:0',
    ]

    if MIRROR_MODE == 'mjpeg':
      # 低延迟路线（默认）：每帧都是一张独立的 JPEG，没有任何帧间依赖，
      # 编码器不用攒缓冲、播放端也不用等切片，端到端只有 ~0.3 秒。
      # 输出到本地 TCP，web_server.py 连上来读，再转发给浏览器。
      ffmpeg_args += ['-vf', f'{vf},format=yuvj420p',
                      '-c:v', 'mjpeg',
                      '-q:v', str(MIRROR_QUALITY),
                      '-f', 'mpjpeg',
                      '-flush_packets', '1',
                      f'tcp://127.0.0.1:{MIRROR_TCP_PORT}?listen=1']
    else:
      # 兼容路线：切成 HLS 片段放到 /tmp（内存盘，不磨损 eMMC），VLC/Safari 都能放
      hls_path = MIRROR_URL[len('hls://'):] if MIRROR_URL.startswith('hls://') else '/tmp/ui_mirror/live.m3u8'
      try:
        os.makedirs(os.path.dirname(hls_path) or '.', exist_ok=True)
      except Exception:
        pass
      # 关键帧间隔跟切片长度对齐，HLS 才能在片段边界干净切开
      gop = max(1, int(round(MIRROR_FPS * MIRROR_SEG_SEC)))
      ffmpeg_args += ['-vf', f'{vf},format=yuv420p',
                      '-c:v', MIRROR_ENCODER,
                      '-g', str(gop)]
      if MIRROR_ENCODER == 'libx264':
        ffmpeg_args += ['-preset', 'ultrafast', '-tune', 'zerolatency',
                        '-pix_fmt', 'yuv420p',
                        '-b:v', MIRROR_BITRATE, '-maxrate', MIRROR_BITRATE,
                        '-bufsize', MIRROR_BITRATE]
      ffmpeg_args += ['-f', 'hls',
                      '-hls_time', str(MIRROR_SEG_SEC),
                      '-hls_list_size', '3',
                      '-hls_flags', 'delete_segments+independent_segments+omit_endlist',
                      '-hls_segment_type', 'mpegts',
                      hls_path]

    # 上一轮还在收尾就不重复启动，绝不能让两个线程往同一个管道里写。
    # 注意：只有"旧进程还活着"才算在收尾 —— ffmpeg 意外退出时它的写线程会一直卡在
    # 队列上不退出，只看 is_alive() 会把重启永久拦住。
    old_proc = getattr(self, "_ffmpeg_proc", None)
    old_thread = getattr(self, "_ffmpeg_thread", None)
    if old_thread is not None and old_thread.is_alive() and old_proc is not None and old_proc.poll() is None:
      return

    try:
      self._ffmpeg_proc = subprocess.Popen(ffmpeg_args, stdin=subprocess.PIPE)
    except Exception:
      cloudlog.exception("ui mirror: failed to start ffmpeg")
      self._ffmpeg_proc = None
      return

    self._ffmpeg_queue = queue.Queue(maxsize=30)
    self._ffmpeg_stop_event = threading.Event()
    self._ffmpeg_thread = threading.Thread(target=self._ffmpeg_writer_thread, daemon=True)
    self._ffmpeg_thread.start()

  def _stop_ui_mirror(self):
    """收掉 ffmpeg。不等待、不阻塞渲染循环。"""
    # 先把句柄抓到手，再置空，否则下面就没东西可关了
    proc = getattr(self, "_ffmpeg_proc", None)
    queue_ = getattr(self, "_ffmpeg_queue", None)
    stop_event = getattr(self, "_ffmpeg_stop_event", None)
    self._ffmpeg_proc = None
    try:
      if stop_event is not None:
        stop_event.set()
      if queue_ is not None:
        queue_.put_nowait(None)   # 写线程收到哨兵就退出
    except Exception:
      pass
    if proc is not None:
      try:
        proc.stdin.close()
      except Exception:
        pass
      try:
        proc.terminate()
      except Exception:
        pass
      # 交给下一轮 tick 回收，别在这里等（等会把渲染循环卡住）
      reap = getattr(self, "_ui_mirror_reap", None)
      if reap is None:
        reap = self._ui_mirror_reap = []
      reap.append((proc, time.monotonic()))

  def _update_ui_mirror(self):
    """每秒最多同步一次：让 ffmpeg 的生死跟着开关走。"""
    if RECORD or not MIRROR_AVAILABLE:
      return

    now = time.monotonic()
    if now - getattr(self, "_ui_mirror_last_sync", 0.0) < 1.0:
      return
    self._ui_mirror_last_sync = now

    # 回收上一轮退掉的 ffmpeg，免得攒一堆僵尸进程
    reap = getattr(self, "_ui_mirror_reap", None)
    if reap:
      for proc, started in list(reap):
        if proc.poll() is not None:
          reap.remove((proc, started))
        elif now - started > 5.0:
          try:
            proc.kill()
          except Exception:
            pass
          reap.remove((proc, started))

    proc = getattr(self, "_ffmpeg_proc", None)
    running = proc is not None and proc.poll() is None

    # ffmpeg 自己挂了（写不了文件、参数不对……）就把残留收干净，下一轮再重来。
    # 不收的话写线程会一直挂着，_start_ui_mirror 的保护会永久拦住重启。
    if proc is not None and not running:
      cloudlog.error(f"ui mirror: ffmpeg exited (rc={proc.returncode}), will retry")
      self._stop_ui_mirror()
      return

    if ui_mirror_enabled():
      if not running:
        self._start_ui_mirror()
    elif running:
      self._stop_ui_mirror()

''' + METHODS_ANCHOR

GRAB_OLD = '''        if RECORD:
          image = rl.load_image_from_texture(self._render_texture.texture)
          data_size = image.width * image.height * 4
          data = bytes(rl.ffi.buffer(image.data, data_size))
          self._ffmpeg_queue.put(data)  # Async write via background thread
          rl.unload_image(image)
'''

GRAB_NEW = GRAB_OLD + '''        elif MIRROR_AVAILABLE:
          self._update_ui_mirror()
          _proc = getattr(self, "_ffmpeg_proc", None)
          if _proc is not None and _proc.poll() is None:
            # 按目标帧率抽帧，避免每帧回读 GPU 拖垮 UI
            interval = max(1, int(round(self._target_fps / max(1, MIRROR_FPS))))
            if self._frame % interval == 0:
              image = rl.load_image_from_texture(self._render_texture.texture)
              data_size = image.width * image.height * 4
              data = bytes(rl.ffi.buffer(image.data, data_size))
              try:
                # 推流慢或对方没连上就丢帧，绝不能阻塞渲染循环
                self._ffmpeg_queue.put_nowait(data)
              except queue.Full:
                pass
              rl.unload_image(image)
'''

# 屏幕熄灭时 openpilot 会直接 continue，取帧代码在 yield 之后永远走不到 —— 镜像跟着黑。
# _should_render 由 ui_state._set_awake 驱动（点火 / 无操作超时 / PC），
# 所以停车、台架上没点火时 30 秒后屏幕一灭，ffmpeg 就再也起不来了。
RENDER_GATE_OLD = """        # Skip rendering when screen is off
        if not self._should_render:
          if PC:
            rl.poll_input_events()
          time.sleep(1 / self._target_fps)
          yield False, 0.0, 0.0
          continue
"""

RENDER_GATE_NEW = """        # Skip rendering when screen is off
        # ui mirror: 镜像开着的时候例外 —— 屏幕灭了也要继续渲染，否则车机上跟着一起黑
        if not self._should_render and not ui_mirror_enabled():
          if PC:
            rl.poll_input_events()
          # ui mirror: 这一支在 yield 之前就 continue 了，后面取帧那一段的
          # _update_ui_mirror() 永远轮不到。少了这一句，就会出现：
          # 屏幕关着的时候把开关关掉 -> ffmpeg 收不到停止信号 -> 一直挂在后台烧 CPU
          #（实测约半个核）。补一次调用，它会看到开关已关，把 ffmpeg 收干净。
          if MIRROR_AVAILABLE:
            self._update_ui_mirror()
          time.sleep(1 / self._target_fps)
          yield False, 0.0, 0.0
          continue
"""

EDITS = [
    ("环境变量与开关逻辑", ENV_BLOCK, ENV_NEW),
    ("渲染到纹理", TRY_TEXTURE_OLD, TRY_TEXTURE_NEW),
    ("ffmpeg 按需启停", METHODS_ANCHOR, METHODS_NEW),
    ("取帧与丢帧保护", GRAB_OLD, GRAB_NEW),
    ("屏幕熄灭时仍然渲染", RENDER_GATE_OLD, RENDER_GATE_NEW),
]

# ---------------------------------------------- 另外两个文件：开关本身 + 参数注册

PARAMS_OLD = '''def ensure_bytes(v):
  return v.encode() if isinstance(v, str) else v
'''

PARAMS_NEW = PARAMS_OLD + '''
# prebuilt 设备上跑的 libparams_c.so 是在 UiMirrorEnabled 出现之前编好的，
# 而 launch_chffrplus.sh 因为有 `prebuilt` 标记会跳过 ./build.py，所以原生 key 表里查不到。
# params 的存储本身就是按文件名来的（/data/params/ 下一个 key 一个文件），与这张表无关，
# 所以在这里放行，省得每次更新完还得在设备上重编 native 代码。
_KEYS_UNKNOWN_TO_PREBUILT_LIBPARAMS = {
    b"UiMirrorEnabled",        # ui mirror toggle (openpilot/tools/ui-mirror)
    b"SunnyconfPairingCode",   # sunnyconf 自带
}
'''

PARAMS_CHECK_OLD = '''    if b"\\0" in key or not params_check_key(self.p, key):
      raise UnknownKeyName(key)'''

PARAMS_CHECK_NEW = '''    if key in _KEYS_UNKNOWN_TO_PREBUILT_LIBPARAMS:
      return key
    if b"\\0" in key or not params_check_key(self.p, key):
      raise UnknownKeyName(key)'''

# ---- 影子存储：让这些 key 活过 manager 的 clear_all() ----
# 只放行 check_key() 还不够。common/params.cc 的 clearAll() 长这样：
#     auto it = keys.find(de->d_name);
#     if (it == keys.end() || (it->second.flags & key_flag)) unlink(...);
# 也就是说「不在这张 native key 表里」的文件一律删掉，而 system/manager/manager.py
# 在启动时、以及每次 onroad / offroad / 点火切换时都会调它（第 33-36 行、148-154 行）。
# 现场表现：开关打开 -> 上车 -> clear_all(CLEAR_ON_IGNITION_ON) -> 参数没了 -> 镜像自动关。
# 设备是 prebuilt（launch_chffrplus.sh 看到 prebuilt 标记就跳过 build.py），不想在设备上重编
# native，于是在 Python 层留个影子副本，clear_all() 之后再放回去。
PARAMS_SHADOW_OLD = '''_KEYS_UNKNOWN_TO_PREBUILT_LIBPARAMS = {
    b"UiMirrorEnabled",        # ui mirror toggle (openpilot/tools/ui-mirror)
    b"SunnyconfPairingCode",   # sunnyconf 自带
}
'''

PARAMS_SHADOW_NEW = PARAMS_SHADOW_OLD + '''
# 影子存储：见上面的说明。存在 /data 下而不是 /tmp —— 设备重启也会走一遍 clear_all()，
# 影子丢了参数就真丢了。
_PARAM_SHADOW_DIR = Path("/data/ui-mirror/params")


def _shadow_path(key) -> Path:
  return _PARAM_SHADOW_DIR / ensure_bytes(key).decode("utf-8", "replace")


def _shadow_save(key, value: bytes) -> None:
  # 目录可能是 root 建的：mkdir / chmod 都当 best-effort，改不动就跳过。
  # 参数本体写在 /data/params/d/ 下，影子写失败只是少一层保险，不能因此抛错。
  try:
    _PARAM_SHADOW_DIR.mkdir(parents=True, exist_ok=True)
  except Exception:
    pass
  try:
    _PARAM_SHADOW_DIR.chmod(0o777)
  except Exception:
    pass
  try:
    p = _shadow_path(key)
    p.write_bytes(value)
    try:
      p.chmod(0o666)
    except Exception:
      pass
  except Exception as e:
    cloudlog.warning(f"param shadow: failed to save {key}: {e}")


def _shadow_load(key):
  try:
    return _shadow_path(key).read_bytes()
  except Exception:
    return None


def _shadow_forget(key) -> None:
  try:
    _shadow_path(key).unlink()
  except Exception:
    pass


def _restore_unknown_keys(params) -> None:
  """clear_all() 刚把它们删了，照影子副本放回去。"""
  for key in _KEYS_UNKNOWN_TO_PREBUILT_LIBPARAMS:
    value = _shadow_load(key)
    if value is None:
      continue
    try:
      params_put(params.p, key, value, len(value), True)
    except Exception as e:
      cloudlog.warning(f"param shadow: failed to restore {key}: {e}")
'''

PARAMS_PUT_OLD = """    k = self.check_key(key)
    value = self._put_cast(k, dat)
    params_put(self.p, k, value, len(value), block)
"""

PARAMS_PUT_NEW = """    k = self.check_key(key)
    value = self._put_cast(k, dat)
    params_put(self.p, k, value, len(value), block)
    if k in _KEYS_UNKNOWN_TO_PREBUILT_LIBPARAMS:
      _shadow_save(k, value)
"""

PARAMS_PUT_BOOL_OLD = """  def put_bool(self, key, val, block=False):
    params_put_bool(self.p, self.check_key(key), val, block)
"""

PARAMS_PUT_BOOL_NEW = """  def put_bool(self, key, val, block=False):
    k = self.check_key(key)
    params_put_bool(self.p, k, val, block)
    if k in _KEYS_UNKNOWN_TO_PREBUILT_LIBPARAMS:
      _shadow_save(k, b"1" if val else b"0")
"""

PARAMS_REMOVE_OLD = """  def remove(self, key):
    params_remove(self.p, self.check_key(key))
"""

PARAMS_REMOVE_NEW = """  def remove(self, key):
    k = self.check_key(key)
    params_remove(self.p, k)
    if k in _KEYS_UNKNOWN_TO_PREBUILT_LIBPARAMS:
      _shadow_forget(k)
"""

PARAMS_CLEAR_OLD = """  def clear_all(self, tx_flag=ParamKeyFlag.ALL):
    params_clear_all(self.p, int(tx_flag))
"""

PARAMS_CLEAR_NEW = """  def clear_all(self, tx_flag=ParamKeyFlag.ALL):
    params_clear_all(self.p, int(tx_flag))
    # 原生 clearAll() 会把「不在 libparams key 表里」的文件一并 unlink，
    # 于是 prebuilt 设备上 UiMirrorEnabled 这类 key 一上下电就消失。
    # 清理之后用影子副本把它们放回去。
    _restore_unknown_keys(self)
"""

DEVICE_IMPORT_OLD = "from openpilot.selfdrive.ui.mici.widgets.button import BigButton, BigCircleButton\n"
DEVICE_IMPORT_NEW = "from openpilot.selfdrive.ui.mici.widgets.button import BigButton, BigCircleButton, BigParamControl\n"

DEVICE_LIST_OLD = "    self._scroller.add_widgets([\n"
DEVICE_LIST_NEW = '''    # ui-mirror: 把完整 UI 画面推到车机大屏，服务端见 openpilot/tools/ui-mirror/
    ui_mirror_toggle = BigParamControl("ui mirror", "UiMirrorEnabled")

''' + DEVICE_LIST_OLD

DEVICE_ROW_OLD = '''      cabin_cam_btn,
      terms_btn,'''
DEVICE_ROW_NEW = '''      cabin_cam_btn,
      ui_mirror_toggle,
      terms_btn,'''

# ---- launch_env.sh：给「可调参数」找个不会被更新冲掉的落脚点 ----
# application.py 里的 MIRROR_* 是 os.getenv 读的，但环境变量得有人设。
# 踩过的坑：给 comma.service 加 systemd drop-in 是没用的 —— comma.service 起的是
# 常驻 tmux server，新 session 继承的是 server 启动那一刻的环境，之后的改动一律看不到。
# 能稳定生效的只有 launch_chffrplus.sh 第 5 行的 `source "$DIR/launch_env.sh"`
# （每次 launch 都重新读一遍）。
# 真正调参的地方是 /data/ui-mirror/mirror.env：/data/ui-mirror/ 不归 openpilot 更新管，
# 改完重启 openpilot 生效，更新也冲不掉。
LAUNCH_ENV_MARK = "# ui-mirror config (added by apply_mirror_patch.py)"

LAUNCH_ENV_OLD = '''export STAGING_ROOT="/data/safe_staging"
'''

LAUNCH_ENV_NEW = LAUNCH_ENV_OLD + '''
# ui-mirror config (added by apply_mirror_patch.py)
# 可调参数（帧率/画质/模式等）写在 /data/ui-mirror/mirror.env，
# 改完 `sudo systemctl restart comma.service` 生效。见 openpilot/tools/ui-mirror/README.md
#
# set -a = allexport：里面的赋值自动导出。少了它会踩坑 ——
# `MIRROR_FPS=30` 只是个 shell 变量，不进 environ，python 的 os.getenv 读不到。
if [ -f /data/ui-mirror/mirror.env ]; then
  set -a
  . /data/ui-mirror/mirror.env
  set +a
fi
'''

# (相对仓库根目录的路径, 幂等标记, [(说明, old, new), ...])
EXTRA_FILES = [
    ("openpilot/common/params.py", "UiMirrorEnabled", [
        ("参数注册", PARAMS_OLD, PARAMS_NEW),
        ("影子存储 helper", PARAMS_SHADOW_OLD, PARAMS_SHADOW_NEW),
        ("check_key 放行", PARAMS_CHECK_OLD, PARAMS_CHECK_NEW),
        ("put 时留副本", PARAMS_PUT_OLD, PARAMS_PUT_NEW),
        ("put_bool 时留副本", PARAMS_PUT_BOOL_OLD, PARAMS_PUT_BOOL_NEW),
        ("remove 时清副本", PARAMS_REMOVE_OLD, PARAMS_REMOVE_NEW),
        ("clear_all 之后恢复", PARAMS_CLEAR_OLD, PARAMS_CLEAR_NEW),
    ]),
    ("openpilot/selfdrive/ui/mici/layouts/settings/device.py", "UiMirrorEnabled", [
        ("import 开关组件", DEVICE_IMPORT_OLD, DEVICE_IMPORT_NEW),
        ("创建开关", DEVICE_LIST_OLD, DEVICE_LIST_NEW),
        ("放进设置列表", DEVICE_ROW_OLD, DEVICE_ROW_NEW),
    ]),
    ("launch_env.sh", LAUNCH_ENV_MARK, [
        ("引入镜像配置", LAUNCH_ENV_OLD, LAUNCH_ENV_NEW),
    ]),
]


def try_compile(src: str, name: str) -> str | None:
  """语法自检，返回错误信息（没问题则返回 None）。"""
  try:
    compile(src, name, "exec")
  except SyntaxError as e:
    return f"{e}\n  line {e.lineno}: {e.text}"
  return None


def patch_extra_files(basedir: str) -> None:
  """顺手把「设置页开关」和「参数注册」也补上。

  这两个文件不是必须有（比如跑的不是 comma four 的 UI），所以失败只警告，不影响主补丁。
  """
  root = Path(basedir)
  if not root.exists():
    print(f"\n[!] 找不到仓库目录 {basedir}，跳过设置页开关的安装")
    print("    开关就用不了了，改成 MIRROR=1 常开同样能用（见 README）")
    return

  print()
  for rel, marker, edits in EXTRA_FILES:
    target = root / rel
    if not target.exists():
      print(f"[!] 跳过 {rel}（文件不存在，版本不同？）")
      continue

    src = target.read_bytes().decode("utf-8")
    if marker in src:
      print(f"[✓] {rel} 已经打过了")
      continue

    changed = 0
    for name, old, new in edits:
      if src.count(old) != 1:
        print(f"[!] {rel} 的「{name}」没改成功（匹配 {src.count(old)} 次，需要正好 1 次）")
        continue
      src = src.replace(old, new, 1)
      changed += 1

    if not changed:
      continue

    # launch_env.sh 是 shell 脚本，只有 python 文件才需要编译自检
    if rel.endswith(".py"):
      err = try_compile(src, rel)
      if err:
        print(f"[!] {rel} 改完语法检查没过，跳过：{err}")
        continue

    backup = target.with_suffix(target.suffix + ".mirror.bak")
    if not backup.exists():
      shutil.copy2(target, backup)
    target.write_bytes(src.encode("utf-8"))
    print(f"[✓] 已更新 {rel}（备份 -> {backup}）")


def main() -> int:
  parser = argparse.ArgumentParser(description="给 openpilot UI 打 UI-mirror 补丁")
  parser.add_argument("target", nargs="?", default=DEFAULT_TARGET, help="application.py 路径")
  parser.add_argument("--basedir", default=DEFAULT_BASEDIR, help="openpilot 仓库根目录（用于装设置页开关）")
  parser.add_argument("--uninstall", action="store_true", help="从备份恢复原文件")
  args = parser.parse_args()

  target = Path(args.target)
  backup = target.with_suffix(target.suffix + ".mirror.bak")

  if not target.exists():
    print(f"[x] 找不到文件: {target}")
    print("    如果 openpilot 不在默认位置，请把完整路径作为参数传入。")
    return 1

  if args.uninstall:
    if not backup.exists():
      print(f"[x] 没有找到备份文件 {backup}")
      return 1
    shutil.copy2(backup, target)
    print(f"[✓] 已恢复原文件: {target}")
    return 0

  # 用二进制读写，避免不同系统把换行符改掉
  src = target.read_bytes().decode("utf-8")

  if MARKER in src:
    print(f"[✓] application.py 已经打过补丁了，跳过。")
    patch_extra_files(args.basedir)
    return 0

  if not backup.exists():
    shutil.copy2(target, backup)
    print(f"[✓] 已备份原文件 -> {backup}")

  failed = []
  for name, old, new in EDITS:
    if src.count(old) != 1:
      failed.append((name, src.count(old)))
      continue
    src = src.replace(old, new, 1)
    print(f"[✓] 已修改: {name}")

  if failed:
    target.write_bytes(src.encode("utf-8"))
    print("\n[!] 下面几处没改成功（你的 openpilot 版本和补丁预期的不一致）：")
    for name, n in failed:
      print(f"    - {name}: 匹配到 {n} 次（需要正好 1 次）")
    print("\n    文件已被部分修改，请对照 README.md 的「手动补丁」小节补上，")
    print(f"    或者执行 python3 {Path(__file__).name} --uninstall 回退。")
    return 2

  # 语法自检
  tmp = target.with_suffix(".mirrorcheck.py")
  tmp.write_bytes(src.encode("utf-8"))
  proc = subprocess.run([sys.executable, "-m", "py_compile", str(tmp)], capture_output=True, text=True)
  tmp.unlink(missing_ok=True)
  if proc.returncode != 0:
    print("[x] 补丁后语法检查没过，未写入原文件：")
    print(proc.stderr)
    return 3

  target.write_bytes(src.encode("utf-8"))
  print(f"\n[✓] UI 补丁完成: {target}")

  # 设置页的开关（comma four = mici UI）
  patch_extra_files(args.basedir)

  print("\n    接下来把开关打开就行：设置 → 设备 → ui mirror（或重启一次 openpilot）")
  return 0


if __name__ == "__main__":
  sys.exit(main())
