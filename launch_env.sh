#!/usr/bin/env bash

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1

# models get lower priority than ui
# - ui is ~5ms
# - modeld is 20ms
# - DM is 10ms
# in order to run ui at 60fps (16.67ms), we need to allow
# it to preempt the model workloads. we have enough
# headroom for this until ui is moved to the CPU.
export QCOM_PRIORITY=12

if [ -z "$AGNOS_VERSION" ]; then
  export AGNOS_VERSION="19.7"
fi

export STAGING_ROOT="/data/safe_staging"

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
