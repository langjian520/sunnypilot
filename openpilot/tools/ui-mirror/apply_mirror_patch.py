#!/usr/bin/env python3
"""
给 openpilot 的 UI 打补丁，让渲染好的完整界面（车道线 / 置信球 / 提示图标）
实时推到网络上，而不是只在 1.9 寸屏上显示。

原理：复用 openpilot 自带的 RECORD 录屏管道（每帧从渲染纹理取像素 -> 管道喂 ffmpeg），
把它改成"抽帧 + 推流"，并且取帧失败/推流慢时丢弃帧而不是卡住 UI。

用法（在 comma 设备上、以 root 运行）：
  python3 apply_mirror_patch.py                     # 打补丁（默认路径）
  python3 apply_mirror_patch.py /path/to/application.py
  python3 apply_mirror_patch.py --uninstall         # 从备份恢复

幂等：已打过补丁会直接提示退出；每次修改前自动备份为 <file>.mirror.bak

配合这些环境变量使用：
  MIRROR=1                              开关
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
MARKER = "MIRROR_FPS"

# ---------------------------------------------------------------- 补丁片段

ENV_BLOCK = '''OFFSCREEN = os.getenv("OFFSCREEN") == "1"  # Disable FPS limiting for fast offline rendering
'''

ENV_NEW = ENV_BLOCK + '''
# --- ui mirror (added by apply_mirror_patch.py) ---
MIRROR = os.getenv("MIRROR") == "1"                      # 开关
MIRROR_URL = os.getenv("MIRROR_URL", "rtsp://127.0.0.1:8554/ui")
MIRROR_FPS = int(os.getenv("MIRROR_FPS", "15"))          # 推流帧率，UI 本身仍是 60fps
MIRROR_SCALE = float(os.getenv("MIRROR_SCALE", "1.0"))   # 画面缩放，负载高就调小
MIRROR_BITRATE = os.getenv("MIRROR_BITRATE", "1200k")
MIRROR_ENCODER = os.getenv("MIRROR_ENCODER", "libx264")  # 或 h264_v4l2m2m（硬编，看设备是否支持）
MIRROR_EXTRA_VF = os.getenv("MIRROR_EXTRA_VF", "")       # 画面方向不对时: transpose=1 或 hflip 等
# --- end ui mirror ---
'''

TRY_TEXTURE_OLD = "      needs_render_texture = self._scale != 1.0 or BURN_IN_MODE or RECORD\n"
TRY_TEXTURE_NEW = "      needs_render_texture = self._scale != 1.0 or BURN_IN_MODE or RECORD or MIRROR\n"

FFMPEG_ANCHOR = "        self._ffmpeg_thread.start()\n"

FFMPEG_NEW = FFMPEG_ANCHOR + '''
      elif MIRROR:
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
          # 不装 mediamtx 时的备选：直接切片到目录，再用 python -m http.server 发出去
          hls_path = MIRROR_URL[len('hls://'):]
          os.makedirs(os.path.dirname(hls_path) or '.', exist_ok=True)
          ffmpeg_args += ['-f', 'hls', '-hls_time', '1', '-hls_list_size', '3',
                          '-hls_flags', 'delete_segments+independent_segments+omit_endlist',
                          hls_path]
        else:
          ffmpeg_args += ['-f', 'mpegts', MIRROR_URL]
        self._ffmpeg_proc = subprocess.Popen(ffmpeg_args, stdin=subprocess.PIPE)
        self._ffmpeg_queue = queue.Queue(maxsize=30)
        self._ffmpeg_stop_event = threading.Event()
        self._ffmpeg_thread = threading.Thread(target=self._ffmpeg_writer_thread, daemon=True)
        self._ffmpeg_thread.start()
'''

GRAB_OLD = '''        if RECORD:
          image = rl.load_image_from_texture(self._render_texture.texture)
          data_size = image.width * image.height * 4
          data = bytes(rl.ffi.buffer(image.data, data_size))
          self._ffmpeg_queue.put(data)  # Async write via background thread
          rl.unload_image(image)
'''

GRAB_NEW = '''        if RECORD or MIRROR:
          # 推流时按需抽帧，避免每帧回读 GPU 拖垮 UI
          interval = 1 if RECORD else max(1, int(round(self._target_fps / max(1, MIRROR_FPS))))
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
    ("环境变量", ENV_BLOCK, ENV_NEW),
    ("渲染到纹理", TRY_TEXTURE_OLD, TRY_TEXTURE_NEW),
    ("ffmpeg 推流参数", FFMPEG_ANCHOR, FFMPEG_NEW),
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
    print(f"[✓] 看起来已经打过补丁了（文件里已有 {MARKER}），无需重复执行。")
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
  print("    接下来: sudo systemctl restart comma   （或重启设备）")
  return 0


if __name__ == "__main__":
  sys.exit(main())
