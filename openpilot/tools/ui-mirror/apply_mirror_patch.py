#!/usr/bin/env python3
"""
给 openpilot 的 UI 打补丁，让渲染好的完整界面（车道线 / 置信球 / 提示图标）
实时推到网络上，而不是只在 1.9 寸屏上显示。

原理：复用 openpilot 自带的 RECORD 录屏管道（每帧从渲染纹理取像素 -> 管道喂 ffmpeg），
改成"抽帧 + 推流 + 按需启停"，并且取帧失败/推流慢时丢弃帧而不是卡住 UI。

开关：设置 → 设备 → "ui mirror"（底层是 params 的 UiMirrorEnabled，见 README）。
      打开后 1 秒内 ffmpeg 自动起来，关掉自动收掉，不用重启 openpilot。

用法（在 comma 设备上、以 root 运行）：
  python3 apply_mirror_patch.py                     # 打补丁（默认路径）
  python3 apply_mirror_patch.py /path/to/application.py
  python3 apply_mirror_patch.py --uninstall         # 从备份恢复

幂等：已打过补丁会直接提示退出；每次修改前自动备份为 <file>.mirror.bak

调参用这些环境变量（写在 comma.service 的 drop-in 里）：
  MIRROR=1                              强制常开（忽略设置里的开关）
  MIRROR=0                              彻底关掉（连代码路径都不走）
  MIRROR_URL=rtsp://127.0.0.1:8554/ui   推给 mediamtx（延迟最低）
                 udp://车机IP:1234      直接 MPEG-TS 推过去
                 hls:///tmp/hls/ui.m3u8 切片到目录，连服务端都不用装的备选方案
  MIRROR_FPS=15                         推流帧率，UI 本身照旧 60fps
  MIRROR_SCALE=1.0                      画面缩放，压力大就调小
  MIRROR_BITRATE=1200k
  MIRROR_ENCODER=libx264                或 h264_v4l2m2m（硬件编码，看设备支不支持）
  MIRROR_EXTRA_VF=                      画面方向不对时填 transpose=1 / hflip 等
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_TARGET = "/data/openpilot/openpilot/system/ui/lib/application.py"
MARKER = "# --- ui mirror (added by apply_mirror_patch.py) ---"

# ---------------------------------------------------------------- 补丁片段

ENV_BLOCK = '''OFFSCREEN = os.getenv("OFFSCREEN") == "1"  # Disable FPS limiting for fast offline rendering
'''

ENV_NEW = ENV_BLOCK + '''
# --- ui mirror (added by apply_mirror_patch.py) ---
MIRROR_ENV = os.getenv("MIRROR", "")                # "1"=强制开、"0"=彻底关、不写=听设置里的开关
MIRROR_AVAILABLE = MIRROR_ENV != "0"                # 是否具备推流能力
MIRROR_URL = os.getenv("MIRROR_URL", "rtsp://127.0.0.1:8554/ui")
MIRROR_FPS = int(os.getenv("MIRROR_FPS", "15"))     # 推流帧率，UI 本身仍是 60fps
MIRROR_SCALE = float(os.getenv("MIRROR_SCALE", "1.0"))  # 画面缩放，负载高就调小
MIRROR_BITRATE = os.getenv("MIRROR_BITRATE", "1200k")
MIRROR_ENCODER = os.getenv("MIRROR_ENCODER", "libx264")  # 或 h264_v4l2m2m（硬编，看设备是否支持）
MIRROR_EXTRA_VF = os.getenv("MIRROR_EXTRA_VF", "")       # 画面方向不对时: transpose=1 或 hflip 等

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
    ffmpeg_args = [
      'ffmpeg',
      '-v', 'warning',
      '-nostats',
      '-f', 'rawvideo',
      '-pix_fmt', 'rgba',
      '-s', f'{self._scaled_width}x{self._scaled_height}',
      '-r', str(MIRROR_FPS),
      '-i', 'pipe:0',
      '-vf', f'vflip{scale_filter}{extra_filter},format=yuv420p',
      '-c:v', MIRROR_ENCODER,
    ]
    if MIRROR_ENCODER == 'libx264':
      ffmpeg_args += ['-preset', 'ultrafast', '-tune', 'zerolatency', '-crf', '30',
                      '-g', str(MIRROR_FPS * 2), '-pix_fmt', 'yuv420p']
    ffmpeg_args += ['-b:v', MIRROR_BITRATE, '-maxrate', MIRROR_BITRATE,
                    '-fflags', 'nobuffer', '-flush_packets', '1']
    if MIRROR_URL.startswith('rtsp://'):
      ffmpeg_args += ['-rtsp_transport', 'tcp', '-f', 'rtsp', MIRROR_URL]
    elif MIRROR_URL.startswith('hls://'):
      hls_path = MIRROR_URL[len('hls://'):]
      os.makedirs(os.path.dirname(hls_path) or '.', exist_ok=True)
      ffmpeg_args += ['-f', 'hls', '-hls_time', '1', '-hls_list_size', '3',
                      '-hls_flags', 'delete_segments+independent_segments+omit_endlist',
                      hls_path]
    else:
      ffmpeg_args += ['-f', 'mpegts', MIRROR_URL]

    # 上一轮还在收尾就等下一秒，绝不能让两个线程往同一个管道里写
    old_thread = getattr(self, "_ffmpeg_thread", None)
    if old_thread is not None and old_thread.is_alive():
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

EDITS = [
    ("环境变量与开关逻辑", ENV_BLOCK, ENV_NEW),
    ("渲染到纹理", TRY_TEXTURE_OLD, TRY_TEXTURE_NEW),
    ("ffmpeg 按需启停", METHODS_ANCHOR, METHODS_NEW),
    ("取帧与丢帧保护", GRAB_OLD, GRAB_NEW),
]


def main() -> int:
  parser = argparse.ArgumentParser(description="给 openpilot UI 打 UI-mirror 补丁")
  parser.add_argument("target", nargs="?", default=DEFAULT_TARGET, help="application.py 路径")
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
    print(f"[✓] 看起来已经打过补丁了（文件里已有 ui mirror 标记），无需重复执行。")
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
  print(f"\n[✓] 补丁完成: {target}")
  print("    接下来: 设置 → 设备 → ui mirror 打开即可（或重启 openpilot）")
  return 0


if __name__ == "__main__":
  sys.exit(main())
