#!/usr/bin/env python3
"""Пакетная разметка папки с картинками через PixAI Tagger v1.0.
Пример: python tag_folder.py /путь/к/картинкам --model ./pixai-tagger-v1.0
Для каждой картинки рядом пишется .txt с тегами через запятую, плюс общий tags.csv.
Уже размеченные картинки (есть .txt) пропускаются, так что скрипт можно прерывать и запускать снова.
"""
import argparse
import csv
import os
import sys
import time
from pathlib import Path

# ===== SETTINGS =====
EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
ORDER = ["rating", "character", "copyright", "style", "general", "meta"]
DEFAULT_BATCH_SIZE = 1


# ===== GFX OVERRIDE =====
def apply_gfx_override():
    gfx = os.environ.get("HSA_OVERRIDE_GFX_VERSION", "").strip()
    if not gfx:
        os.environ.pop("HSA_OVERRIDE_GFX_VERSION", None)
        return ""
    os.environ["HSA_OVERRIDE_GFX_VERSION"] = gfx
    return gfx


# ===== SPLIT PENDING FILES INTO BATCHES =====
def chunked(items, size):
    if size < 1:
        raise ValueError("batch size must be at least 1")
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


# ===== TURN PIPELINE OUTPUT INTO ONE RESULT PER IMAGE =====
def unwrap_results(raw):
    results = raw["results"]
    if isinstance(results, dict):
        return [results]
    return list(results)


def tag_or_error(tagger, image):
    try:
        return tagger(image)["results"]
    except Exception as error:
        return error


def tag_images(tagger, images):
    if not images:
        return []
    if len(images) == 1:
        return [tag_or_error(tagger, images[0])]
    try:
        raw = tagger.postprocess(tagger.forward(tagger.preprocess(images)))
        results = unwrap_results(raw)
    except Exception as error:
        print(f"[ошибка] пачка из {len(images)}: {error}; повторяю по одной", file=sys.stderr)
        return [tag_or_error(tagger, image) for image in images]
    if len(results) != len(images):
        print(
            f"[ошибка] пачка вернула {len(results)} вместо {len(images)}; повторяю по одной",
            file=sys.stderr,
        )
        return [tag_or_error(tagger, image) for image in images]
    return results


def categories_of(result):
    return {name: sorted(result.get(name, {}), key=lambda tag: -result[name][tag]) for name in ORDER}


# ===== LOAD THE MODEL =====
def load_tagger(model):
    import torch
    from transformers import pipeline

    device = 0 if torch.cuda.is_available() else -1
    if device == 0:
        print("Устройство:", torch.cuda.get_device_name(0))
    else:
        print("Устройство:", "CPU (видеокарта не найдена!)")
    tagger = pipeline(
        model=model,
        image_processor=model,
        trust_remote_code=True,
        device=device,
        dtype=torch.float16 if device == 0 else torch.float32,
    )
    return tagger


# ===== TAG THE FOLDER =====
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--model", default="pixai-labs/pixai-tagger-v1.0")
    ap.add_argument("--csv", default="tags.csv")
    ap.add_argument("--no-txt", action="store_true", help="не писать .txt рядом с картинками")
    ap.add_argument("--force", action="store_true", help="переразметить уже размеченные")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = ap.parse_args()
    if args.batch_size < 1:
        print("Размер пачки должен быть 1 или больше.", file=sys.stderr)
        return 1

    gfx = apply_gfx_override()
    if gfx:
        print("HSA_OVERRIDE_GFX_VERSION:", gfx)
    print("Пачка:", args.batch_size)

    tagger = load_tagger(args.model)
    from PIL import Image

    files = sorted(path for path in Path(args.folder).rglob("*") if path.suffix.lower() in EXTS)
    pending = []
    for index, path in enumerate(files, 1):
        if path.with_suffix(".txt").exists() and not args.force:
            continue
        pending.append((index, path))

    csv_path = Path(args.csv)
    new_csv = not csv_path.exists()
    started = time.time()
    done = 0
    with open(csv_path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new_csv:
            writer.writerow(["file"] + ORDER)
        for batch in chunked(pending, args.batch_size):
            loaded = []
            for index, path in batch:
                try:
                    loaded.append((index, path, Image.open(path).convert("RGB")))
                except Exception as error:
                    print(f"[ошибка] {path}: {error}", file=sys.stderr)
            results = tag_images(tagger, [image for _, _, image in loaded])
            for (index, path, image), result in zip(loaded, results):
                image.close()
                if isinstance(result, Exception):
                    print(f"[ошибка] {path}: {result}", file=sys.stderr)
                    continue
                cats = categories_of(result)
                if not args.no_txt:
                    path.with_suffix(".txt").write_text(
                        ", ".join(tag for name in ORDER for tag in cats[name]),
                        encoding="utf-8",
                    )
                writer.writerow([str(path)] + [", ".join(cats[name]) for name in ORDER])
                done += 1
                if done % 50 == 0:
                    handle.flush()
                    rate = done / (time.time() - started)
                    print(f"{index}/{len(files)}  {rate:.2f} карт./с, осталось ~{(len(files) - index) / rate / 3600:.1f} ч")
    print("Готово, размечено:", done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
