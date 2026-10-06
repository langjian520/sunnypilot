#!/usr/bin/env bash
# 在 comma 设备上执行：给 openpilot UI 打「画面镜像」补丁，并装好配套服务
#
#   bash install.sh              自动安装（有 mediamtx 走 RTSP，没有就自动用 HLS 备选方案）
#   bash install.sh --hls        强制用 HLS 备选方案（不依赖 mediamtx）
#   bash install.sh --uninstall  卸载并还原
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=/opt/ui-mirror
UI_FILE=/data/openpilot/openpilot/system/ui/lib/application.py
DROPIN_DIR=/etc/systemd/system/comma.service.d
COMMA_SERVICE=/usr/lib/systemd/system/comma.service
HLS_DIR=/tmp/hls
HLS_PORT=8000

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()  { printf '    \033[32m✓\033[0m %s\n' "$*"; }
bad() { printf '    \033[31m✗\033[0m %s\n' "$*"; }

if [ "$(id -u)" != "0" ]; then
  echo "请用 root 运行： sudo bash install.sh"; exit 1
fi

# ------------------------------------------------------------------ 卸载
if [[ "${1:-}" == "--uninstall" ]]; then
  say "卸载 ui-mirror"
  systemctl disable --now ui-mirror.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/ui-mirror.service "$DROPIN_DIR/10-ui-mirror.conf"
  rm -rf "$INSTALL_DIR" "$HLS_DIR"
  if [ -f "$UI_FILE.mirror.bak" ]; then
    cp "$UI_FILE.mirror.bak" "$UI_FILE"
    ok "UI 补丁已还原"
  fi
  systemctl daemon-reload
  systemctl restart comma.service >/dev/null 2>&1 || true
  ok "完成，openpilot 已重启"
  exit 0
fi

# ------------------------------------------------------------------ 0. 检查环境
say "检查环境"
[ -f "$UI_FILE" ] || { bad "找不到 $UI_FILE（openpilot 路径不对？）"; exit 1; }
ok "找到 UI 源码"

command -v ffmpeg >/dev/null 2>&1 || {
  bad "设备上没有 ffmpeg"
  echo "    先执行: sudo /usr/comma/apt_setup.sh && sudo apt-get update && sudo apt-get install -y ffmpeg"
  exit 1
}
ok "ffmpeg: $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"

[ -f "$COMMA_SERVICE" ] || { bad "找不到 $COMMA_SERVICE，无法注入环境变量"; exit 1; }
ok "openpilot 服务存在"

# ------------------------------------------------------------------ 1. 决定走哪条路
say "准备服务端"
mkdir -p "$INSTALL_DIR"
cp "$SRC_DIR/apply_mirror_patch.py" "$SRC_DIR/hls_server.py" "$INSTALL_DIR/"

MODE=hls
if [[ "${1:-}" == "--hls" ]]; then
  echo "    已指定 --hls，跳过 mediamtx"
elif [ -f "$SRC_DIR/bin/mediamtx" ]; then
  cp "$SRC_DIR/bin/mediamtx" "$INSTALL_DIR/mediamtx"
  cp "$SRC_DIR/mediamtx.yml" "$INSTALL_DIR/mediamtx.yml"
  chmod +x "$INSTALL_DIR/mediamtx"
  MODE=rtsp
  ok "使用随包附带的 mediamtx"
elif [ -f "$INSTALL_DIR/mediamtx" ]; then
  MODE=rtsp
  ok "已有 mediamtx，继续用"
else
  echo "    没有 mediamtx，试着联网下载…"
  MT_URL="https://github.com/bluenviron/mediamtx/releases/download/v1.21.1/mediamtx_v1.21.1_linux_arm64.tar.gz"
  if command -v curl >/dev/null 2>&1 && curl -fsSL --max-time 120 "$MT_URL" -o /tmp/mediamtx.tgz 2>/dev/null; then
    tar -xzf /tmp/mediamtx.tgz -C /tmp mediamtx 2>/dev/null && mv /tmp/mediamtx "$INSTALL_DIR/mediamtx"
    rm -f /tmp/mediamtx.tgz
    cp "$SRC_DIR/mediamtx.yml" "$INSTALL_DIR/mediamtx.yml"
    chmod +x "$INSTALL_DIR/mediamtx"
    MODE=rtsp
    ok "下载成功"
  else
    bad "下载失败（国内访问 GitHub 通常不行）"
    echo "    改用 HLS 备选方案：不装任何服务端，浏览器直接看。"
  fi
fi

# ------------------------------------------------------------------ 2. 服务与地址
say "注册 ui-mirror 服务"
if [ "$MODE" = "rtsp" ]; then
  MIRROR_URL="rtsp://127.0.0.1:8554/ui"
  cat > /etc/systemd/system/ui-mirror.service <<EOF
[Unit]
Description=comma UI mirror stream server (mediamtx)
After=network.target
Before=comma.service

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/mediamtx $INSTALL_DIR/mediamtx.yml
Restart=always
RestartSec=2
KillMode=mixed

[Install]
WantedBy=multi-user.target
EOF
else
  MIRROR_URL="hls://$HLS_DIR/ui.m3u8"
  mkdir -p "$HLS_DIR"
  PY="$(command -v python3)"
  cat > /etc/systemd/system/ui-mirror.service <<EOF
[Unit]
Description=comma UI mirror HLS server
After=network.target
Before=comma.service

[Service]
Type=simple
WorkingDirectory=$HLS_DIR
ExecStartPre=/bin/mkdir -p $HLS_DIR
ExecStart=$PY $INSTALL_DIR/hls_server.py $HLS_DIR $HLS_PORT
Restart=always
RestartSec=2
KillMode=mixed

[Install]
WantedBy=multi-user.target
EOF
fi

systemctl daemon-reload
systemctl enable --now ui-mirror.service
sleep 1
if systemctl is-active --quiet ui-mirror.service; then
  ok "ui-mirror 已运行（模式: $MODE）"
else
  bad "ui-mirror 没起来，看日志: journalctl -u ui-mirror -n 50"
fi

# ------------------------------------------------------------------ 3. 环境变量
say "给 openpilot 注入环境变量"
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_DIR/10-ui-mirror.conf" <<EOF
[Service]
# 总开关在设备上：设置 → 设备 → "ui mirror"（底层是 params 的 UiMirrorEnabled）
# 想跳过开关直接常开，就加一行: Environment="MIRROR=1"
Environment="MIRROR_URL=$MIRROR_URL"
Environment="MIRROR_FPS=15"
Environment="MIRROR_BITRATE=1200k"
Environment="MIRROR_ENCODER=libx264"
# 画面方向不对就把下面这行的注释去掉（transpose=1/2/3、hflip、vflip 逐个试）
# Environment="MIRROR_EXTRA_VF=transpose=1"
EOF
ok "写入 $DROPIN_DIR/10-ui-mirror.conf"

# ------------------------------------------------------------------ 4. 打补丁
say "给 UI 打补丁"
python3 "$INSTALL_DIR/apply_mirror_patch.py" "$UI_FILE" || {
  bad "补丁失败，请看上面的提示，或照 README.md 的「手动补丁」小节改"
  exit 1
}

# ------------------------------------------------------------------ 5. 重启生效
say "重启 openpilot"
systemctl daemon-reload
systemctl restart comma.service
sleep 8

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$IP" ] && IP="$(ip -br addr show wlan0 2>/dev/null | awk '{print $3}' | cut -d/ -f1)"
[ -z "$IP" ] && IP="<设备IP>"

echo
echo "---------------------------------------------------------------"
echo "装好了（模式: $MODE，设备 IP: $IP）。"
echo
echo "打开开关（二选一）："
echo "  ① 设备上: 设置 → 设备 → ui mirror"
echo "  ② 命令行: python3 -c \"from openpilot.common.params import Params; Params().put_bool('UiMirrorEnabled', True)\""
echo
echo "然后在车机上打开："
if [ "$MODE" = "rtsp" ]; then
  echo "  RTSP（VLC / MX Player，推荐）: rtsp://$IP:8554/ui"
  echo "  浏览器 HLS                  : http://$IP:8888/ui/index.m3u8"
  echo "  浏览器 WebRTC（最低延迟）   : http://$IP:8889/ui"
else
  echo "  浏览器打开: http://$IP:$HLS_PORT/ui.m3u8"
  echo "  （部分播放器要直接给地址，VLC 里选「网络串流」粘进去即可）"
fi
echo
echo "画面可能要等 10~20 秒才出来——openpilot 的 UI 进程要重启一次。"
echo "---------------------------------------------------------------"
