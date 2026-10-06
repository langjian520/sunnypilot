#!/usr/bin/env bash
# openpilot UI mirror —— 补丁自愈
#
# 为什么需要这个：
#   AGNOS 的 overlay 更新流程在 system/updated/updated.py 的 finalize_update() 里会
#     shutil.copytree(merged -> finalized)
#     git reset --hard                      # ← 本地改动在这一步被丢掉
#     git submodule foreach ... git reset --hard
#   然后 launch_chffrplus.sh 把 finalizaed 整个替换掉 /data/openpilot。
#   结果就是：openpilot 一更新，我们打进去的补丁全没了，而且没有任何提示。
#
#   这个脚本定期检查补丁还在不在，没了就补回来。
#
# 保守原则：
#   - 行驶中（IsOffroad != 1）只补文件，绝不重启设备
#   - 重启有 10 分钟防抖，避免补丁反复失败时把设备拖进重启循环
#
# 手动跑一次： sudo bash /data/ui-mirror/selfheal.sh

PREFIX="${UI_MIRROR_PREFIX:-/data/ui-mirror}"
BASEDIR="${UI_MIRROR_BASEDIR:-/data/openpilot}"
APP="$BASEDIR/openpilot/system/ui/lib/application.py"
PATCHER="$PREFIX/apply_mirror_patch.py"
LOG="$PREFIX/selfheal.log"
STAMP="$PREFIX/.last_restart"
MARKER="ui mirror (added by apply_mirror_patch.py)"
DEVMARK="$BASEDIR/.git/.ui_mirror_devmode"
DEBOUNCE=600

log() { printf '%s %s\n' "$(date -Is)" "$*" >> "$LOG"; }
as_comma() { sudo -u comma "$@"; }

[ -f "$APP" ] || exit 0

# 打完补丁后留一个「本地开发」标记：launch_chffrplus.sh 看到 .git 里有比 .overlay_init 新的
# 文件就会跳过 overlay 覆盖（这是 openpilot 自己设计的「别动我的本地改动」信号）。
mark_dev() { as_comma touch "$DEVMARK" 2>/dev/null || true; }

if grep -qF "$MARKER" "$APP"; then
  mark_dev          # 补丁还在，只顺手刷新标记
  exit 0
fi

# ---- 补丁没了 ----
[ -f "$PATCHER" ] || { log "patch missing but $PATCHER not found, giving up"; exit 0; }
log "patch missing in $APP, re-applying"

if ! as_comma python3 "$PATCHER" "$APP" --basedir "$BASEDIR" >> "$LOG" 2>&1; then
  log "patch re-apply FAILED (see above)"
  exit 0
fi
mark_dev
log "patch re-applied"

# ---- 补丁要重启 openpilot 才生效 ----
if [ "$(cat /data/params/d/IsOffroad 2>/dev/null || echo '')" != "1" ]; then
  log "not offroad, skip restart (patch will take effect at next openpilot start)"
  exit 0
fi

now=$(date +%s)
last=$(cat "$STAMP" 2>/dev/null || echo 0)
case "$last" in ''|*[!0-9]*) last=0 ;; esac
if [ $((now - last)) -lt "$DEBOUNCE" ]; then
  log "restart skipped (debounced, last was $((now - last))s ago)"
  exit 0
fi

printf '%s\n' "$now" > "$STAMP"
log "restarting comma.service to activate patch"
systemctl restart comma.service >> "$LOG" 2>&1
log "restart issued"
