#!/usr/bin/env bash
# openpilot 完整 UI -> 车机大屏镜像   一键安装
#
# 在 comma 设备上以 root 运行：
#   sudo bash install.sh
#
# 装完在车机/手机/电脑浏览器打开：http://<设备IP>:8000/
#
# 做了什么：
#   1. 装一份完整版 ffmpeg 到 /data/ui-mirror/bin/
#      （comma 自带的 /usr/local/venv/bin/ffmpeg 是 openpilot 精简构建，
#        只认 file/pipe 协议、没有 mpjpeg/hls 封装器，发不出去）
#   2. 装低延迟网页服务并设为开机自启（systemd: ui-mirror-web）
#      主通道是 MJPEG（端到端约 0.1 秒），HLS 通道保留给老浏览器兜底
#   3. 写可调参数 /data/ui-mirror/mirror.env（帧率/画质/模式，30 帧）
#   4. 给 openpilot 打补丁：渲染帧 -> ffmpeg -> 网页
#      打补丁的顺序很重要：openpilot/common/params.py 必须先补上「影子存储」，
#      否则参数会被 manager 的 clear_all() 删掉（详见 selfheal.sh 顶部注释）
#   5. 装补丁自愈定时器（systemd: ui-mirror-selfheal.timer）
#   6. 重启 openpilot 让补丁生效
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${UI_MIRROR_PREFIX:-/data/ui-mirror}"
HLS_DIR="/tmp/ui_mirror"
PORT="${UI_MIRROR_PORT:-8000}"
SERVICE=ui-mirror-web
SELFHEAL=ui-mirror-selfheal

# openpilot 仓库根目录：优先用参数，其次自动探测
BASEDIR="${1:-}"
if [ -z "$BASEDIR" ]; then
  for d in /data/openpilot /data/openpilot/openpilot; do
    if [ -f "$d/openpilot/system/ui/lib/application.py" ]; then BASEDIR="$d"; break; fi
  done
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '\033[92m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[93m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[91m[x]\033[0m %s\n' "$*"; exit 1; }

[ "$(id -u)" = "0" ] || die "请用 root 运行：sudo bash install.sh"

if [ -z "$BASEDIR" ] || [ ! -f "$BASEDIR/openpilot/system/ui/lib/application.py" ]; then
  die "找不到 openpilot 仓库，请手动指定：sudo bash install.sh /data/openpilot"
fi
ok "openpilot 仓库：$BASEDIR"

# openpilot 的代码是 comma 用户在跑；用 root 去写会让文件属主变成 root，
# 之后 manager 的 save_bootlog 复制参数目录时会因权限报错、直接把 manager 打挂。
as_comma() { sudo -u comma "$@"; }

# ------------------------------------------------------------------ 1. ffmpeg
mkdir -p "$PREFIX/bin" "$PREFIX/web" "$PREFIX/params" "$HLS_DIR"
# HLS 目录是 openpilot 的 UI 进程（comma 用户）在写，必须让它可写，
# 否则 ffmpeg 切片失败、镜像永远出不来
chown comma:comma "$HLS_DIR" "$PREFIX/params" 2>/dev/null || true

# 设备的 /tmp 是只有 150MB 的 tmpfs，而解压完整版 ffmpeg 只要 ~60MB 临时空间，
# 再叠上传包副本和 mktemp 残留就爆「No space left on device」（2026-10-09 真机踩到，
# 当时 /tmp 直接 100%）。把临时目录固定到 $PREFIX 所在的盘（通常是 /data，89G），
# 一劳永逸 —— 即使调用者没设 TMPDIR 也不会踩。
export TMPDIR="$PREFIX/.tmp"
mkdir -p "$TMPDIR"

# 注意：不能写成 ffmpeg ... | grep -q，grep 一匹配到就退出会让 ffmpeg 吃 SIGPIPE，
# 配合 set -o pipefail 会被误判成失败。所以先把输出抓进变量再用 case 匹配。
# 设备自带的 openpilot 精简版 ffmpeg 这三样全都没有，所以必须换完整版：
#   mpjpeg + mjpeg 编码器 -> 低延迟主通道      hls + libx264 -> 兼容回退通道
#   注意必须先把输出抓进变量再匹配：用管道接 grep -q 的话，grep 一命中就退出，
#   ffmpeg 收到 SIGPIPE 被杀，配合 set -o pipefail 会被误判成"能力缺失"。
ffmpeg_ok() {
  [ -x "$1" ] || return 1
  local mux enc proto
  mux="$("$1" -hide_banner -muxers 2>/dev/null || true)"
  case "$mux" in *' mpjpeg '*|*' mpjpeg'*) ;; *) return 1 ;; esac
  case "$mux" in *' hls '*) ;; *) return 1 ;; esac
  enc="$("$1" -hide_banner -encoders 2>/dev/null || true)"
  case "$enc" in *mjpeg*) ;; *) return 1 ;; esac
  case "$enc" in *libx264*) ;; *) return 1 ;; esac
  proto="$("$1" -hide_banner -protocols 2>/dev/null || true)"
  case "$proto" in *tcp*) ;; *) return 1 ;; esac
  return 0
}

if ffmpeg_ok "$PREFIX/bin/ffmpeg"; then
  ok "ffmpeg 已就绪（完整版）"
else
  say "安装完整版 ffmpeg ..."
  TARBALL=""
  for c in "$HERE/ffmpeg-arm64.tar.xz" "$HERE"/ffmpeg-*-arm64-static.tar.xz /data/ffmpeg-arm64.tar.xz; do
    if [ -f "$c" ]; then TARBALL="$c"; break; fi
  done

  SRC=""
  if [ -n "$TARBALL" ]; then
    say "  解压 $TARBALL"
    TMPD="$(mktemp -d)"
    tar -xJf "$TARBALL" -C "$TMPD"
    SRC="$(find "$TMPD" -maxdepth 2 -name ffmpeg -type f | head -1)"
  else
    # 没带安装包就现下（设备网速慢的话建议在电脑上下好一起传过来）
    warn "没找到 ffmpeg-arm64.tar.xz，尝试直接下载 ..."
    TMPD="$(mktemp -d)"
    URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz"
    if curl -fsSL -m 900 -o "$TMPD/f.tar.xz" "$URL"; then
      tar -xJf "$TMPD/f.tar.xz" -C "$TMPD"
      SRC="$(find "$TMPD" -maxdepth 2 -name ffmpeg -type f | head -1)"
    else
      die "下载失败。请在电脑上下载 $URL ，和本脚本放在同一目录后重试"
    fi
  fi

  [ -n "$SRC" ] || die "压缩包里没找到 ffmpeg 可执行文件"
  install -m 755 "$SRC" "$PREFIX/bin/ffmpeg"
  rm -rf "$TMPD"
  ffmpeg_ok "$PREFIX/bin/ffmpeg" || die "装好的 ffmpeg 不满足要求（缺 mpjpeg / mjpeg / hls / libx264 / tcp 里的某一项）"
  ok "ffmpeg -> $PREFIX/bin/ffmpeg（完整版）"
fi

# ------------------------------------------------------- 2. 网页服务 + systemd
install -m 644 "$HERE/web_server.py" "$PREFIX/web_server.py"
install -m 644 "$HERE/web/index.html" "$PREFIX/web/index.html"
install -m 644 "$HERE/web/hls.min.js" "$PREFIX/web/hls.min.js"
# 自愈脚本和补丁脚本放 /data（不在 openpilot 里），这样 openpilot 被更新覆盖也不影响自愈
install -m 755 "$HERE/apply_mirror_patch.py" "$PREFIX/apply_mirror_patch.py"
install -m 755 "$HERE/apply_mirror_supersample.py" "$PREFIX/apply_mirror_supersample.py"
install -m 755 "$HERE/selfheal.sh" "$PREFIX/selfheal.sh"
# safe_restart.sh 必须装上：它是唯一安全的重启入口。裸跑 systemctl restart comma.service
# 会踩到 comma.sh 的 tap-reset（恢复出厂设置）分支 —— 2026-10-09 真机事故。
install -m 755 "$HERE/safe_restart.sh" "$PREFIX/safe_restart.sh"
ok "网页服务 + 自愈脚本 + 安全重启 -> $PREFIX/"

# AGNOS 的根分区默认是只读挂载（ro），写 systemd unit 之前必须先 remount rw
ROOT_WAS_RO=0
if ! touch /etc/systemd/system/.ui-mirror-wtest 2>/dev/null; then
  if mount -o remount,rw / 2>/dev/null; then
    ROOT_WAS_RO=1
    ok "根分区已临时改为可写"
  else
    die "根分区只读且无法 remount rw，装不了 systemd 服务"
  fi
fi
rm -f /etc/systemd/system/.ui-mirror-wtest

install -m 644 "$HERE/ui-mirror-web.service" "/etc/systemd/system/$SERVICE.service"
install -m 644 "$HERE/ui-mirror-selfheal.service" "/etc/systemd/system/$SELFHEAL.service"
install -m 644 "$HERE/ui-mirror-selfheal.timer" "/etc/systemd/system/$SELFHEAL.timer"
# 留一份到 /data（系统更新重刷 rootfs 后，重跑本脚本就能恢复）
cp -f "$HERE/ui-mirror-web.service" "$HERE/ui-mirror-selfheal.service" "$HERE/ui-mirror-selfheal.timer" "$PREFIX/"

systemctl daemon-reload
systemctl enable --now "$SERVICE" >/dev/null 2>&1 || true
systemctl enable --now "$SELFHEAL.timer" >/dev/null 2>&1 || true
sleep 1
systemctl is-active --quiet "$SERVICE" \
  && ok "网页服务已启动：$SERVICE（端口 $PORT，开机自启）" \
  || warn "网页服务没起来，看日志：journalctl -u $SERVICE -n 50 --no-pager"
systemctl is-active --quiet "$SELFHEAL.timer" \
  && ok "自愈定时器已启动：$SELFHEAL.timer（每 5 分钟检查一次补丁还在不在）" \
  || warn "自愈定时器没起来，看日志：systemctl status $SELFHEAL.timer"

if [ "$ROOT_WAS_RO" = "1" ]; then
  mount -o remount,ro / 2>/dev/null || true
  ok "根分区已恢复只读"
fi

# ------------------------------------------------------------------ 3. 打补丁
say ""
say "给 openpilot 打补丁 ..."
as_comma python3 "$PREFIX/apply_mirror_patch.py" \
  "$BASEDIR/openpilot/system/ui/lib/application.py" \
  --basedir "$BASEDIR" || warn "补丁脚本返回非 0，请检查上面的输出"

# 「本地开发」标记：launch_chffrplus.sh 看到 .git 里有比 .overlay_init 新的文件，
# 就会跳过 overlay 覆盖，我们的补丁才不会在下次重启时被 openpilot 的更新流程冲掉。
as_comma touch "$BASEDIR/.git/.ui_mirror_devmode" 2>/dev/null || true

# ------------------------------------------------------------------ 4. 开关
# 没设置过才默认打开；用户自己关过就尊重用户
if [ ! -f /data/params/d/UiMirrorEnabled ]; then
  as_comma env PYTHONPATH="$BASEDIR" /usr/local/venv/bin/python -c \
    "from openpilot.common.params import Params; Params().put_bool('UiMirrorEnabled', True, block=True)" \
    >/dev/null 2>&1 && ok "镜像开关已默认打开（设置 -> 设备 -> ui mirror）" \
    || warn "开关默认值没写进去，请在设置里手动打开"
else
  ok "镜像开关保持原样：$(cat /data/params/d/UiMirrorEnabled 2>/dev/null)"
fi

# ------------------------------------------------- 4.5 可调参数（帧率 / 画质 / 模式）
# application.py 里的 MIRROR_* 是 os.getenv 读的，得有地方设。踩过的坑：
#   * 给 comma.service 加 systemd drop-in 没用 —— comma.service 起的是常驻 tmux server，
#     新 session 继承的是 server 启动那一刻的环境，之后的改动它看不到。
#   * 真正每次启动都重读的只有 launch_chffrplus.sh 第 5 行的 `source launch_env.sh`，
#     所以补丁往 launch_env.sh 末尾挂了一行：把 /data/ui-mirror/mirror.env source 进来。
# /data/ui-mirror/ 不归 openpilot 更新管，所以放在这儿的参数不会被更新冲掉。
ENVFILE="$PREFIX/mirror.env"
if [ -f "$ENVFILE" ]; then
  ok "可调参数保持原样：$ENVFILE（MIRROR_FPS=$(sed -n 's/^MIRROR_FPS=//p' "$ENVFILE" | tail -1)）"
else
  cat > "$ENVFILE" <<'ENVEOF'
# ui-mirror 可调参数
#
# ⚠️ 改完**不要**直接 `sudo systemctl restart comma.service`。设备开机后被摸过 5 次
#    以上屏幕时（touch_count > 4），裸重启会命中 /usr/comma/comma.sh 的 tap-reset
#    分支，**恢复出厂设置，/data 全清**。一律用：
#        sudo bash /data/ui-mirror/safe_restart.sh
#    （2026-10-09 真机踩过一次。原因见 safe_restart.sh 头部注释。）
#
# 每行都要写 export（launch_env.sh 那侧虽然开了 set -a 兜底，但别依赖它）
#
# MIRROR_FPS          推流帧率。UI 本身始终 60fps，这里只决定回读+编码多少帧。
#                     30 = 更跟手；15 = 省一半 CPU；10 = 最省
# MIRROR_QUALITY      MJPEG 画质，2 最好 / 31 最省流量
# MIRROR_MODE         mjpeg = 低延迟（默认） / hls = 兼容老浏览器
# MIRROR_SCALE        画面缩放，负载高就调小，如 0.75（插值放大，不会多出细节）
# MIRROR_RENDER_SCALE ⚠️ 已废弃，不要设。UI 内部超采样倍数，2026-10-09 真机实测失败：
#                     打完补丁后画面只渲染在纹理左上 1/4，设备屏上看起来像屏幕坏了
#                     （splash/UI 挤在左侧、文字被截断）。原因见 README 的
#                     「分辨率 / 清晰度」一节。保持默认 1.0 即可。
export MIRROR_FPS=30
export MIRROR_QUALITY=6
export MIRROR_MODE=mjpeg
ENVEOF
  chown comma:comma "$ENVFILE" 2>/dev/null || true
  ok "可调参数已写入 $ENVFILE（MIRROR_FPS=30）"
fi

# ------------------------------------------------------------------ 5. 重启
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$IP" ] || IP="<设备IP>"

OFFROAD="$(cat /data/params/d/IsOffroad 2>/dev/null || echo 1)"
say ""
if [ "$OFFROAD" != "1" ]; then
  warn "设备不在停车状态，已跳过重启。停车后执行： sudo bash $PREFIX/safe_restart.sh"
else
  say "重启 openpilot 让补丁生效 ..."
  # 一律走 safe_restart.sh。它会先把 /tmp/booted 补上，让 comma.sh 跳过整段出厂
  # 重置判断。裸跑 systemctl restart comma.service，在 touch_count>4（开机后摸过
  # 5 次以上屏幕）时会直接恢复出厂设置，/data 全清。2026-10-09 真机踩过一次。
  bash "$PREFIX/safe_restart.sh" || warn "重启失败，可手动执行 sudo bash $PREFIX/safe_restart.sh"
fi

cat <<EOF

---------------------------------------------------------------
装好了。车机上这样看（浏览器推荐，不用装 App）：

    http://$IP:$PORT/

  默认是低延迟 MJPEG 通道，端到端约 0.1 秒，画面基本跟手。
  帧率默认 30（实测约 24），嫌发烫就在 mirror.env 里调到 20 或 15。

VLC 也可以打开网络串流（用同一个地址）：

    http://$IP:$PORT/stream.mjpeg

然后在设备上打开开关：

    设置 -> 设备 -> ui mirror

打开约 1 秒出画面，关掉立刻停，都不用重启。
初次装完先等 30 秒左右，等 openpilot 把 UI 重新拉起来。

调参（帧率 / 画质 / 模式 / 超采样倍数）：

    sudo nano $ENVFILE
    sudo bash $PREFIX/safe_restart.sh         # 改完重启生效

  ⚠️ 绝对不要直接 sudo systemctl restart comma.service。
     设备只要开机后被摸过 5 次以上屏幕（touch_count>4），裸重启就会命中
     /usr/comma/comma.sh 里「连点屏幕 = 恢复出厂设置」的分支，/data 会被清空。
     safe_restart.sh 做的就是先补 /tmp/booted 把那段判断跳过。详见该脚本头部。

提高投屏清晰度：

    投屏流的分辨率 = 设备自己那块屏的原生分辨率（mici 是 536x240）。
    「拉大已有画面」（MIRROR_SCALE）和「让 UI 内部超采样」（MIRROR_RENDER_SCALE）
    都试过了：前者不会多出细节，后者 2026-10-09 真机实测会把画面挤到纹理左上 1/4，
    在设备屏上看起来像屏幕坏了，已废弃。

    目前唯一有用且安全的做法是把 JPEG 质量调高：

    sudo nano $ENVFILE              # 把 MIRROR_QUALITY 从 6 改成 2
    sudo bash $PREFIX/safe_restart.sh

排错：
    systemctl status $SERVICE                 # 网页服务
    systemctl status $SELFHEAL.timer          # 自愈定时器
    cat $PREFIX/selfheal.log                  # 自愈日志
    curl -s http://127.0.0.1:$PORT/health     # 上游连上没（connected=True 才对）
    pgrep -af "$PREFIX/bin/ffmpeg"            # ffmpeg 在不在跑
    ss -ltn | grep 8554                       # MJPEG 上游端口在不在听
    systemctl status comma.service            # openpilot 本体
---------------------------------------------------------------
EOF
