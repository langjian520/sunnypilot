#!/usr/bin/env bash
#
# 安全重启 comma.service —— 唯一正确的重启姿势
#
# ============================ 为什么不能直接重启 ============================
#
# /usr/comma/comma.sh 里有一段**开机路径**的恢复出厂设置判断：
#
#   if [ ! -f /tmp/booted ]; then
#     touch /tmp/booted
#     if [ -f "$RESET_TRIGGER" ]; then
#       $RESET
#     elif (( "$(cat /sys/class/input/input2/device/touch_count)" > 4 )); then
#       echo "launching system reset, got taps"
#       $RESET --tap-reset            # ← 恢复出厂设置
#     elif ! mountpoint -q /data; then
#       $RESET --recover
#     fi
#   fi
#
# 原厂意图：开机时连点屏幕 5 下 = 恢复出厂设置（应急恢复功能）。
# 但它有个前提被破坏了：
#
#   1. /tmp 是内存盘，/tmp/booted 这个「本次开机已处理过」的哨兵只在**真开机**时写入；
#      systemd-tmpfiles 还会定期清理 /tmp，所以服务重启时它经常已经不在了。
#   2. touch_count 记的是**本次开机以来的触摸次数**（正常用一会儿就远超 4）。
#
# 两者一叠加 → `systemctl restart comma.service` 就会命中 tap-reset 分支。
# **2026-10-09 在真机上真的发生过一次，设备被恢复出厂设置，/data 全丢。**
#
# 本脚本做的事很简单：重启之前先把 /tmp/booted 补上，让 comma.sh 整段跳过。
# 补上之后 touch_count 是多少都无所谓。
#
# ============================ 用法 ============================
#
#   sudo bash safe_restart.sh            重启（安全）
#   sudo bash safe_restart.sh --check    只体检，不重启
#
# 任何时候需要重启 openpilot（改 mirror.env、重打补丁、换配置），都用这个，
# **不要**再手敲 systemctl restart comma.service。
#
# ============================ 注意 ============================
#
# 真·重启设备（sudo reboot）本身是安全的：开机时 /tmp 是空的，touch_count 也是 0，
# 判断不会命中。只有「重启服务」这种伪开机才会踩到。
#
set -u

BOOTED=/tmp/booted
TOUCH=/sys/class/input/input2/device/touch_count
OFFROAD_FILE=/data/params/d/IsOffroad

say() { printf '%s\n' "$*"; }

if [ "$(id -u)" != "0" ]; then
  say "请用 root 跑： sudo bash $0"
  exit 1
fi

# ---- 1. 体检 ----
tc=0
if [ -r "$TOUCH" ]; then
  tc=$(cat "$TOUCH" 2>/dev/null || echo 0)
fi
case "$tc" in ''|*[!0-9]*) tc=0 ;; esac

if [ -f "$BOOTED" ]; then
  say "[ok] $BOOTED 已存在 —— 重启不会碰到重置判断。"
else
  say "[!] $BOOTED 不存在，touch_count=$tc"
  if [ "$tc" -gt 4 ]; then
    say ""
    say "    ⚠️  现在直接执行 systemctl restart comma.service 会触发【恢复出厂设置】"
    say "    ⚠️  （comma.sh 的 tap-reset 分支：touch_count > 4）"
    say ""
  fi
  say "    正在补上哨兵文件 ..."
  if ! touch "$BOOTED"; then
    say "    补哨兵失败，拒绝继续。"
    exit 1
  fi
  say "    已补上：$BOOTED"
fi

# ---- 2. 复核。哨兵必须真的在，不信任上一步的返回值 ----
if [ ! -f "$BOOTED" ]; then
  say "[x] 复核失败：$BOOTED 仍然不存在，拒绝重启。"
  exit 1
fi
say "[ok] 复核通过，重启是安全的。"

if [ "${1:-}" = "--check" ]; then
  exit 0
fi

# ---- 3. 行驶中不动 ----
offroad=$(cat "$OFFROAD_FILE" 2>/dev/null || echo 1)
if [ "$offroad" != "1" ]; then
  say "[!] 设备不在停车状态（IsOffroad=$offroad），不重启。"
  say "    补丁/config 会在下次 openpilot 启动时生效。"
  exit 0
fi

# ---- 4. 重启 ----
say "重启 comma.service ..."
systemctl restart comma.service
rc=$?
say "systemctl 返回 $rc。"
say "openpilot 大概需要 30-60 秒把 UI 拉起来。"
exit $rc
