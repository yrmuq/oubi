#!/bin/sh
set -eu

# ===== SETTINGS =====
if [ -z "${HSA_OVERRIDE_GFX_VERSION:-}" ]; then
  unset HSA_OVERRIDE_GFX_VERSION
fi
export HF_HOME="${HF_HOME:-/cache}"
export HUGGINGFACE_HUB_CACHE="${HUGGINGFACE_HUB_CACHE:-/cache}"

cd /data
exec python /opt/oubi/oubi_web.py
