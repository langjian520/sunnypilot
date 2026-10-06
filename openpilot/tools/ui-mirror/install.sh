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
#        只认 file/pipe 协议、没有 hls 封装器，发不出去）
#   2. 装 HLS 网页服务并设为开机自启（systemd: ui-mirror-web）
#   3. 给 openpilot 打补丁：渲染帧 -> ffmpeg -> HLS 切片 -> 网页
#      打补丁的顺序很重要：openpilot/common/params.py 必须先补上「影子存储」，
#      否则参数会被 manager 的 clear_all() 删掉（详见 selfheal.sh 顶部注释）
#   4. 装补丁自愈定时器（systemd: ui-mirror-selfheal.timer）
#   5. 重启 openpilot 让补丁生效
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

# 注意：不能写成 ffmpeg ... | grep -q，grep 一匹配到就退出会让 ffmpeg 吃 SIGPIPE，
# 配合 set -o pipefail 会被误判成失败。所以先把输出抓进变量再用 case 匹配。
ffmpeg_ok() {
  [ -x "$1" ] || return 1
  local mux enc
  mux="$("$1" -hide_banner -muxers 2>/dev/null || true)"
  case "$mux" in *' hls '*) ;; *) return 1 ;; esac
  enc="$("$1" -hide_banner -encoders 2>/dev/null || true)"
  case "$enc" in *libx264*) ;; *) return 1 ;; esac
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
  ffmpeg_ok "$PREFIX/bin/ffmpeg" || die "装好的 ffmpeg 不满足要求（缺 hls 或 libx264）"
  ok "ffmpeg -> $PREFIX/bin/ffmpeg（完整版）"
fi

# ------------------------------------------------------- 2. 网页服务 + systemd
install -m 644 "$HERE/web_server.py" "$PREFIX/web_server.py"
install -m 644 "$HERE/web/index.html" "$PREFIX/web/index.html"
install -m 644 "$HERE/web/hls.min.js" "$PREFIX/web/hls.min.js"
# 自愈脚本和补丁脚本放 /data（不在 openpilot 里），这样 openpilot 被更新覆盖也不影响自愈
install -m 755 "$HERE/apply_mirror_patch.py" "$PREFIX/apply_mirror_patch.py"
install -m 755 "$HERE/selfheal.sh" "$PREFIX/selfheal.sh"
ok "网页服务 + 自愈脚本 -> $PREFIX/"

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

# ------------------------------------------------------------------ 5. 重启
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$IP" ] || IP="<设备IP>"

OFFROAD="$(cat /data/params/d/IsOffroad 2>/dev/null || echo 1)"
say ""
if [ "$OFFROAD" != "1" ]; then
  warn "设备不在停车状态，已跳过重启。停车后执行： sudo systemctl restart comma.service"
else
  say "重启 openpilot 让补丁生效 ..."
  systemctl restart comma.service || warn "重启失败，可手动执行 sudo systemctl restart comma.service"
fi

cat <<EOF

---------------------------------------------------------------
装好了。车机上这样看（浏览器推荐，不用装 App）：

    http://$IP:$PORT/

VLC 也可以打开网络串流：

    http://$IP:$PORT/live.m3u8

然后在设备上打开开关：

    设置 -> 设备 -> ui mirror

打开约 1 秒出画面，关掉立刻停，都不用重启。
初次装完先等 30 秒左右，等 openpilot 把 UI 重新拉起来。

排错：
    systemctl status $SERVICE                 # 网页服务
    systemctl status $SELFHEAL.timer          # 自愈定时器
    cat $PREFIX/selfheal.log                  # 自愈日志
    ls -l $HLS_DIR                            # ffmpeg 有没有在切片
    systemctl status comma.service            # openpilot 本体
---------------------------------------------------------------
EOF
