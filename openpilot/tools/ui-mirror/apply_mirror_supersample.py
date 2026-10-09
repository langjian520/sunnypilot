#!/usr/bin/env python3
"""
给已经打过 ui-mirror 补丁的 application.py 再加一层「超采样渲染」。

================================================================================
  🚨 已实测失败，不要用（2026-10-09 真机验证，已回退）
================================================================================

  打完补丁 + 设 MIRROR_RENDER_SCALE=2 之后：
    · 渲染纹理确实变成了 1072x480（ffmpeg -s 能确认，那部分是对的）
    · 但画面内容只画在纹理的左上 1/4，其余全黑
    · 设备屏上表现为 splash/UI 挤在屏幕左侧、文字被硬生生截断
      （"sunnypilot" 只显示成 "sunny"），看起来就像屏幕坏了

  根因：下面第 6/7 处改的 rl_push_matrix() + rl_scalef(2.0)，在原版 application.py
  里属于 `if self._scale != 1.0:` 分支 —— 而默认 SCALE=1.0 时这个分支从来没被执行过，
  是未验证的死代码，在 texture mode 下没能真正生效。

  本脚本保留仅供参考和日后研究。想在车上用，先把 rlgl 的矩阵/裁剪那一层搞明白。

  回退：
    sudo -u comma cp -f <application.py>.supersample.bak <application.py>
    sudo grep -v '^export MIRROR_RENDER_SCALE=' /data/ui-mirror/mirror.env > /tmp/m.env
    sudo cp /tmp/m.env /data/ui-mirror/mirror.env && rm -f /tmp/m.env
    sudo bash /data/ui-mirror/safe_restart.sh
================================================================================

要解决的问题
------------
投屏流的分辨率 = UI 渲染纹理的分辨率 = _scaled_width x _scaled_height。
而 mici（comma four）的 _default_width() 是 536、_default_height() 是 240 ——
正好等于它那块 DSI 屏的原生模式（240x536，旋转后为 536x240）。

也就是说：设备**已经是 1:1 像素完美**地渲染自己的屏，投屏流也忠实地是 536x240。
流本身没有任何缩放损失 —— 只是 536x240 ≈ 13 万像素，投到车机大屏上必然糊。
想更清晰，唯一有效的做法是让 UI **内部**以更高分辨率渲染（超采样），
再把高分辨率帧送去投屏、同时降采样回面板。放大已经存在的 536x240 是没用的
（MIRROR_SCALE 那种 scale 滤镜只是插值，不会凭空长出细节）。

为什么不能直接用 SCALE=2
------------------------
application.py 里 rl.init_window() 用的就是 _scaled_width/_scaled_height。
SCALE 一调大，**窗口**也跟着变大，而 DRM 上只有 240x536 一个模式，
窗口大于面板会直接影响到设备自己那块屏（crop / 失败黑屏）。
这个补丁的做法相反：**窗口尺寸一个字节都不动**，只把渲染纹理放大，
blit 回窗口时由已有的双线性过滤降采样。所以设备屏幕是安全的。

改了什么（8 处）
----------------
1. 新增环境变量 MIRROR_RENDER_SCALE（默认 1.0，与不打补丁时行为完全一致）
2. __init__ 里算出 _rt_width/_rt_height（= _scaled_* x MIRROR_RENDER_SCALE，取偶）
3. load_render_texture() 用 _rt_* ，而 rl.init_window() 保持 _scaled_* 不变
4. RECORD 的 ffmpeg -s 用 _rt_*
5. 投屏的 ffmpeg -s 用 _rt_*
6/7. 渲染矩阵由 _scale 改成 _scale * _rt_scale（push 与 pop 两处）
8. blit 的 src 用 _rt_*，dst 保持 _scaled_*（降采样回面板）

用法（设备上、需要 root 写 /data/openpilot）
--------------------------------------------
  sudo python3 apply_mirror_supersample.py                  # 打补丁
  sudo python3 apply_mirror_supersample.py --uninstall      # 还原
  sudo python3 apply_mirror_supersample.py --check          # 只看状态，不写
然后：
  echo 'export MIRROR_RENDER_SCALE=2' | sudo tee -a /data/ui-mirror/mirror.env
  sudo bash /data/ui-mirror/safe_restart.sh        # 不要裸跑 systemctl restart！

⚠️ 绝对不要直接 `sudo systemctl restart comma.service`：设备开机后被摸过 5 次以上
   屏幕（touch_count > 4）时，裸重启会命中 /usr/comma/comma.sh 的 tap-reset 分支，
   **恢复出厂设置、/data 全清**。safe_restart.sh 会先补 /tmp/booted 跳过那段判断。
   2026-10-09 就是因为这条命中了重置。

代价：回读（glReadPixels）和 JPEG 编码都按倍数的平方增长。
  1x = 536x240   （约 0.5MB/帧 → 15MB/s）
  2x = 1072x480  （约 2MB/帧   → 62MB/s）
  3x = 1608x720  （约 4.6MB/帧 → 139MB/s）
实测 1x 时 UI 进程 52% + ffmpeg 44%（8 核）。2x 是这台设备的合理上限。
"""

import argparse
import shutil
import sys
from pathlib import Path

DEFAULT_TARGET = "/data/openpilot/openpilot/system/ui/lib/application.py"
MARKER = "# --- ui mirror supersample (added by apply_mirror_supersample.py) ---"
BACKUP_SUFFIX = ".supersample.bak"

# --------------------------------------------------------------------------- 8 处改动

ENV_ANCHOR = 'MIRROR_SEG_SEC = float(os.getenv("MIRROR_SEG_SEC", "0.5"))  # hls 模式切片长度，越短延迟越低\n'

ENV_ADD = '''
# --- ui mirror supersample (added by apply_mirror_supersample.py) ---
# UI 内部超采样倍数：1.0 = 跟屏幕 1:1（默认，等于没打这个补丁）。
# 设成 2.0 就是「UI 按 1072x480 渲染，降采样回 536x240 显示在设备屏上，
# 同时把 1072x480 的原始帧送去投屏」。字体和矢量图形是真的多出细节，
# 不是把 536x240 拉大 —— 后者不会更清晰。
# 代价按平方增长：回读和 JPEG 编码都翻 4 倍。这台 8 核设备 2.0 是上限。
MIRROR_RENDER_SCALE = float(os.getenv("MIRROR_RENDER_SCALE", "1.0"))
# --- end ui mirror supersample ---
'''

SCALED_BLOCK_OLD = '''    self._scaled_width += self._scaled_width % 2
    self._scaled_height += self._scaled_height % 2
'''

SCALED_BLOCK_NEW = SCALED_BLOCK_OLD + '''    # --- ui mirror supersample ---
    # 渲染纹理尺寸与窗口尺寸解耦：窗口（= 设备自己那块屏）永远用 _scaled_*，
    # 只有渲染纹理用 _rt_*。这样调超采样倍数不会碰到 DRM 模式。
    self._rt_scale = max(1.0, MIRROR_RENDER_SCALE)
    self._rt_width = int(self._scaled_width * self._rt_scale)
    self._rt_height = int(self._scaled_height * self._rt_scale)
    self._rt_width += self._rt_width % 2
    self._rt_height += self._rt_height % 2
    # --- end ui mirror supersample ---
'''

TEXTURE_OLD = '        self._render_texture = rl.load_render_texture(self._scaled_width, self._scaled_height)\n'
TEXTURE_NEW = '        self._render_texture = rl.load_render_texture(self._rt_width, self._rt_height)\n'

RECORD_S_OLD = "          '-s', f'{self._scaled_width}x{self._scaled_height}',  # Input resolution\n"
RECORD_S_NEW = "          '-s', f'{self._rt_width}x{self._rt_height}',  # Input resolution\n"

MIRROR_S_OLD = """      '-s', f'{self._scaled_width}x{self._scaled_height}',
      '-r', str(MIRROR_FPS),
"""
MIRROR_S_NEW = """      '-s', f'{self._rt_width}x{self._rt_height}',
      '-r', str(MIRROR_FPS),
"""

PUSH_OLD = '''        if self._scale != 1.0:
          rl.rl_push_matrix()
          rl.rl_scalef(self._scale, self._scale, 1.0)
'''
PUSH_NEW = '''        if self._scale != 1.0 or self._rt_scale != 1.0:
          rl.rl_push_matrix()
          rl.rl_scalef(self._scale * self._rt_scale, self._scale * self._rt_scale, 1.0)
'''

POP_OLD = '''        if self._scale != 1.0:
          rl.rl_pop_matrix()
'''
POP_NEW = '''        if self._scale != 1.0 or self._rt_scale != 1.0:
          rl.rl_pop_matrix()
'''

BLIT_OLD = '''          src_rect = rl.Rectangle(0, 0, float(self._scaled_width), -float(self._scaled_height))
          dst_rect = rl.Rectangle(0, 0, float(self._scaled_width), float(self._scaled_height))
'''
BLIT_NEW = '''          src_rect = rl.Rectangle(0, 0, float(self._rt_width), -float(self._rt_height))
          dst_rect = rl.Rectangle(0, 0, float(self._scaled_width), float(self._scaled_height))
'''

EDITS = [
    ("环境变量 MIRROR_RENDER_SCALE", ENV_ANCHOR, ENV_ANCHOR + ENV_ADD),
    ("渲染纹理尺寸 _rt_width/_rt_height", SCALED_BLOCK_OLD, SCALED_BLOCK_NEW),
    ("load_render_texture 用 _rt_*", TEXTURE_OLD, TEXTURE_NEW),
    ("RECORD ffmpeg -s 用 _rt_*", RECORD_S_OLD, RECORD_S_NEW),
    ("投屏 ffmpeg -s 用 _rt_*", MIRROR_S_OLD, MIRROR_S_NEW),
    ("渲染矩阵 push", PUSH_OLD, PUSH_NEW),
    ("渲染矩阵 pop", POP_OLD, POP_NEW),
    ("blit src=_rt_* dst=_scaled_*", BLIT_OLD, BLIT_NEW),
]


def apply_edits(text, reverse=False):
    """Returns (new_text, log). Raises on any anchor that is not exactly once."""
    log = []
    for name, old, new in EDITS:
        if reverse:
            old, new = new, old
        n = text.count(old)
        if n != 1:
            raise SystemExit(
                f"错误：锚点「{name}」在文件里出现 {n} 次（必须正好 1 次）。\n"
                f"文件可能不是预期的版本，或者已经被改过。没有做任何修改。"
            )
        text = text.replace(old, new)
        log.append(f"  ok  {name}")
    return text, log


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("target", nargs="?", default=DEFAULT_TARGET)
    ap.add_argument("--uninstall", action="store_true", help="还原成打补丁前的样子")
    ap.add_argument("--check", action="store_true", help="只报告状态，不写文件")
    args = ap.parse_args()

    p = Path(args.target)
    if not p.is_file():
        raise SystemExit(f"找不到 {p}")

    # newline="" on both ends: no translation, ever. The device's files are LF, and Windows'
    # os.linesep would otherwise quietly rewrite every line ending to CRLF while testing locally.
    with open(p, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    patched = MARKER in text

    if args.check:
        print(f"{p}")
        print(f"  超采样补丁：{'已打' if patched else '未打'}")
        print(f"  基础投屏补丁：{'已打' if 'apply_mirror_patch.py' in text else '未打'}")
        print(f"  行数：{len(text.splitlines())}")
        sys.exit(0)

    if patched and not args.uninstall:
        print("已经打过超采样补丁了，无需重复。")
        sys.exit(0)
    if not patched and args.uninstall:
        print("没有检测到超采样补丁，无需还原。")
        sys.exit(0)
    if not patched and "apply_mirror_patch.py" not in text:
        raise SystemExit("这个文件还没打过基础投屏补丁，请先运行 apply_mirror_patch.py。")

    new_text, log = apply_edits(text, reverse=args.uninstall)

    # 写完之前先确认是合法 Python，避免把 UI 弄成起不来的状态
    try:
        compile(new_text, str(p), "exec")
    except SyntaxError as e:
        raise SystemExit(f"错误：改完不是合法 Python（{e}），没有写文件。")

    if not args.uninstall:
        shutil.copy2(p, str(p) + BACKUP_SUFFIX)
        print(f"已备份 -> {p}{BACKUP_SUFFIX}")

    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)
    print(("已还原：" if args.uninstall else "已打补丁：") + str(p))
    for line in log:
        print(line)
    print()
    if not args.uninstall:
        print("🚨 警告：本补丁 2026-10-09 真机实测失败 —— 打完并设 MIRROR_RENDER_SCALE=2")
        print("   之后画面只会渲染在纹理左上 1/4，设备屏上看起来像屏幕坏了。")
        print("   除非你清楚自己在做什么，否则不要继续。详见本脚本头部注释。")
        print()
    print("接下来：")
    print("  用 safe_restart.sh，不要裸跑 systemctl restart comma.service ——")
    print("  设备开机后被摸过 5 次以上屏幕时，裸重启会触发恢复出厂设置。")
    if args.uninstall:
        print("  sudo bash /data/ui-mirror/safe_restart.sh")
    else:
        print("  echo 'export MIRROR_RENDER_SCALE=2' | sudo tee -a /data/ui-mirror/mirror.env")
        print("  sudo bash /data/ui-mirror/safe_restart.sh")


if __name__ == "__main__":
    main()
