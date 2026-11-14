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


# ===== DESCRIBE A DOWNLOAD =====
def format_bytes(amount):
    value = float(max(0, amount))
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            if unit == "Б":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"


def format_speed(amount):
    return f"{format_bytes(amount)}/с"


def progress_line(done, total, speed):
    if total > 0:
        shown = min(float(done), float(total))
        percent = shown / float(total) * 100
        body = f"Скачивание: {percent:.0f}% ({format_bytes(shown)} из {format_bytes(total)})"
    else:
        shown = float(done)
        body = f"Скачивание: {format_bytes(shown)}"
    if speed is None:
        if total > 0 and shown >= float(total):
            return f"{body}, уже в кэше"
        return f"{body}, скорость ещё считается"
    return f"{body}, сейчас {format_speed(speed)}"


def model_status_line(cached, total):
    if total <= 0:
        return "Модель: размер на Hugging Face неизвестен."
    percent = min(100, float(cached) / float(total) * 100)
    return f"Модель: в кэше {format_bytes(cached)} из {format_bytes(total)} ({percent:.0f}%)."


class DownloadMeter:
    def __init__(self, total, cached=0, clock=None, emit=print, interval=2):
        self.total = total
        self.cached = float(cached)
        self.positions = {}
        self.clock = clock or time.monotonic
        self.emit = emit
        self.interval = interval
        self.last_log = 0.0
        self.last_done = self.cached
        self.last_time = self.clock()

    def done(self):
        return self.cached + sum(self.positions.values())

    def observe(self, filename, position, file_size):
        position = float(position or 0)
        if file_size:
            position = min(position, float(file_size))
        if position <= self.positions.get(filename, 0):
            return
        self.positions[filename] = position
        now = self.clock()
        finished = self.total > 0 and self.done() >= self.total
        due = self.last_log == 0 or now - self.last_log >= self.interval
        if due or finished:
            self.log(now)

    def log(self, now):
        done = self.done()
        span = now - self.last_time
        speed = None if span < 0.5 else (done - self.last_done) / span
        self.last_done = done
        self.last_time = now
        self.last_log = now
        self.emit(progress_line(done, self.total, speed), flush=True)


def byte_progress(meter, filename="file", file_size=0):
    try:
        from tqdm.auto import tqdm as base
    except ImportError:
        base = object

    cap = float(file_size or 0)

    class ByteProgress(base):
        def __init__(self, *args, **kwargs):
            initial = float(kwargs.get("initial") or 0)
            kwargs["disable"] = True
            if base is object:
                self.n = initial
                self.total = kwargs.get("total")
            else:
                super().__init__(*args, **kwargs)
            self._report()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.close()
            return False

        def update(self, n=1):
            if base is not object:
                super().update(n)
            else:
                self.n += float(n or 0)
            self._report()

        def _report(self):
            limit = cap or float(getattr(self, "total", 0) or 0)
            meter.observe(filename, float(getattr(self, "n", 0) or 0), limit)

        def close(self):
            return None

        def refresh(self):
            return None

        def set_description(self, *args, **kwargs):
            return None

        def set_postfix(self, *args, **kwargs):
            return None

        def set_postfix_str(self, *args, **kwargs):
            return None

    return ByteProgress


def rate_limited(error):
    text = str(error).lower()
    return "429" in text or "too many requests" in text or "rate limit" in text


# ===== DOWNLOAD THE MODEL =====
def cached_file(repo_id, filename):
    from huggingface_hub import try_to_load_from_cache

    found = try_to_load_from_cache(repo_id, filename)
    if isinstance(found, str) and os.path.isfile(found):
        return found
    return None


def list_model_files(repo_id):
    from huggingface_hub import HfApi

    info = HfApi().model_info(repo_id, files_metadata=True)
    files = []
    for sibling in info.siblings:
        size = sibling.size
        lfs = getattr(sibling, "lfs", None)
        if size is None and isinstance(lfs, dict):
            size = lfs.get("size")
        files.append((sibling.rfilename, int(size or 0)))
    return files


def download_model(repo_id):
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    print("Сейчас: проверяю файлы модели на Hugging Face.", flush=True)
    files = list_model_files(repo_id)
    total = sum(size for _, size in files)
    cached = 0
    missing = []
    for name, size in files:
        if cached_file(repo_id, name):
            cached += size
        else:
            missing.append((name, size))
    print(model_status_line(cached, total), flush=True)
    if not missing:
        print("Модель уже скачана.", flush=True)
        return
    print(f"Сейчас: скачиваю ещё {format_bytes(max(0, total - cached))}, файлов: {len(missing)}.", flush=True)
    meter = DownloadMeter(total=total, cached=cached)
    from huggingface_hub import hf_hub_download

    try:
        for name, size in missing:
            print(f"Сейчас: файл {name} ({format_bytes(size)}).", flush=True)
            hf_hub_download(repo_id=repo_id, filename=name, tqdm_class=byte_progress(meter, name, size))
            print(f"Файл скачан: {name}", flush=True)
    except Exception as error:
        if rate_limited(error):
            print(
                "Сейчас: Hugging Face ограничил скачивание. Добавь HF_TOKEN и запусти снова. Кэш сохранится.",
                flush=True,
            )
        raise
    print("Модель скачана.", flush=True)


def categories_of(result):
    return {name: sorted(result.get(name, {}), key=lambda tag: -result[name][tag]) for name in ORDER}


# ===== LOAD THE MODEL =====
def load_tagger(model):
    import torch
    from transformers import pipeline

    if os.path.isdir(model):
        print("Сейчас: модель локальная, скачивание не нужно.", flush=True)
        source = model
    else:
        download_model(model)
        source = model
    print("Сейчас: загружаю модель в память. Это может занять несколько минут.", flush=True)
    device = 0 if torch.cuda.is_available() else -1
    if device == 0:
        print("Устройство:", torch.cuda.get_device_name(0), flush=True)
    else:
        print("Устройство: CPU (видеокарта не найдена!)", flush=True)
    tagger = pipeline(
        model=source,
        image_processor=source,
        trust_remote_code=True,
        device=device,
        dtype=torch.float16 if device == 0 else torch.float32,
    )
    print("Модель загружена.", flush=True)
    return tagger


# ===== TAG THE FOLDER =====
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="+")
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
    print("Пачка:", args.batch_size, flush=True)
    print("Сейчас: готовлю модель.", flush=True)

    tagger = load_tagger(args.model)
    from PIL import Image

    print("Сейчас: ищу картинки.", flush=True)
    files = []
    seen = set()
    for folder in args.folder:
        for path in Path(folder).rglob("*"):
            if path.suffix.lower() not in EXTS or path in seen:
                continue
            seen.add(path)
            files.append(path)
    files.sort()
    pending = []
    for index, path in enumerate(files, 1):
        if path.with_suffix(".txt").exists() and not args.force:
            continue
        pending.append((index, path))
    print(
        f"Сейчас: картинок {len(files)}, уже размечено {len(files) - len(pending)}, осталось {len(pending)}.",
        flush=True,
    )
    if not pending:
        print("Сейчас: размечать нечего.", flush=True)

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
            if not loaded:
                continue
            if len(loaded) == 1:
                index, path, _image = loaded[0]
                print(f"Сейчас: размечаю {path} ({index} из {len(files)}).", flush=True)
            else:
                print(
                    f"Сейчас: размечаю пачку {loaded[0][0]}–{loaded[-1][0]} из {len(files)}.",
                    flush=True,
                )
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
                elapsed = time.time() - started
                rate = done / elapsed if elapsed else 0
                left = (len(pending) - done) / rate / 3600 if rate else 0
                percent = done / len(pending) * 100 if pending else 100
                print(
                    f"Разметка: {percent:.0f}% ({done} из {len(pending)}), {rate:.2f} карт./с, осталось ~{left:.1f} ч",
                    flush=True,
                )
    print("Готово, размечено:", done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
