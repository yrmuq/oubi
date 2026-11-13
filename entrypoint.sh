#!/bin/sh
set -eu

# ===== SETTINGS =====
MODE="${OUBI_MODE:-all}"
MODEL="${OUBI_MODEL:-pixai-labs/pixai-tagger-v1.0}"
BATCH_SIZE="${OUBI_BATCH_SIZE:-1}"
NO_TXT="${OUBI_NO_TXT:-0}"
FORCE="${OUBI_FORCE:-0}"
MOVE="${OUBI_MOVE:-0}"
DRY_RUN="${OUBI_DRY_RUN:-0}"
NSFW_FROM="${OUBI_NSFW_FROM:-q}"
COPY_TXT="${OUBI_COPY_TXT:-0}"
CSV_PATH="/data/tags.csv"
GENDERS_PATH="/data/genders.csv"
IMAGES_DIR="/images"
OUTPUT_DIR="/output"

# ===== CHECK VALUES =====
case "$MODE" in
  all|tag|sort) ;;
  *)
    echo "OUBI_MODE must be all, tag, or sort." >&2
    exit 1
    ;;
esac

case "$NSFW_FROM" in
  q|e) ;;
  *)
    echo "OUBI_NSFW_FROM must be q or e." >&2
    exit 1
    ;;
esac

case "$BATCH_SIZE" in
  ""|*[!0-9]*)
    echo "OUBI_BATCH_SIZE must be a whole number, 1 or more." >&2
    exit 1
    ;;
esac
if [ "$BATCH_SIZE" -lt 1 ]; then
  echo "OUBI_BATCH_SIZE must be 1 or more." >&2
  exit 1
fi

flag_on() {
  case "$1" in
    1|true|TRUE|yes|YES) return 0 ;;
    0|false|FALSE|no|NO) return 1 ;;
    *)
      echo "Use 0 or 1, not '$1'." >&2
      exit 1
      ;;
  esac
}

if [ -z "${HSA_OVERRIDE_GFX_VERSION:-}" ]; then
  unset HSA_OVERRIDE_GFX_VERSION
fi
export HF_HOME="${HF_HOME:-/cache}"
export HUGGINGFACE_HUB_CACHE="${HUGGINGFACE_HUB_CACHE:-/cache}"

cd /data

# ===== TAG =====
run_tag() {
  if [ ! -d "$IMAGES_DIR" ]; then
    echo "The images folder is not mounted at /images." >&2
    exit 1
  fi
  set -- /opt/oubi/tag_folder.py "$IMAGES_DIR" --model "$MODEL" --csv "$CSV_PATH" --batch-size "$BATCH_SIZE"
  if flag_on "$NO_TXT"; then
    set -- "$@" --no-txt
  fi
  if flag_on "$FORCE"; then
    set -- "$@" --force
  fi
  python "$@"
}

# ===== SORT =====
run_sort() {
  if [ ! -f "$CSV_PATH" ]; then
    echo "No tags.csv in /data yet. Run tagging first." >&2
    exit 1
  fi
  if [ ! -d "$OUTPUT_DIR" ]; then
    echo "The output folder is not mounted at /output." >&2
    exit 1
  fi
  set -- /opt/oubi/sort_images.py --csv "$CSV_PATH" --out "$OUTPUT_DIR" --nsfw-from "$NSFW_FROM"
  if [ -f "$GENDERS_PATH" ]; then
    set -- "$@" --genders "$GENDERS_PATH"
  fi
  if flag_on "$MOVE"; then
    set -- "$@" --move
  fi
  if flag_on "$DRY_RUN"; then
    set -- "$@" --dry-run
  fi
  if flag_on "$COPY_TXT"; then
    set -- "$@" --copy-txt
  fi
  python "$@"
}

# ===== RUN THE SELECTED MODE =====
case "$MODE" in
  tag) run_tag ;;
  sort) run_sort ;;
  all)
    run_tag
    run_sort
    ;;
esac
