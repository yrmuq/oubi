#!/usr/bin/env python3
"""Пакетная разметка папки с картинками через PixAI Tagger v1.0.
Пример: python tag_folder.py /путь/к/картинкам --model ./pixai-tagger-v1.0
Для каждой картинки .txt с тегами пишется в подпапку tags, плюс общий tags.csv.
Уже размеченные картинки (есть .txt) пропускаются, так что скрипт можно прерывать и запускать снова.
"""
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

from oubi_files import image_files, sidecar_path, stored_image_path
from oubi_gpu import GpuError
from oubi_index import report_changes
from oubi_nvidia import GPU_UUID, nvidia_device
from oubi_rocm import amd_device

# ===== SETTINGS =====
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
        print(say(40, count=len(images), error=error), file=sys.stderr)
        return [tag_or_error(tagger, image) for image in images]
    if len(results) != len(images):
        print(
            say(41, got=len(results), expected=len(images)),
            file=sys.stderr,
        )
        return [tag_or_error(tagger, image) for image in images]
    return results


# ===== DESCRIBE A DOWNLOAD =====
def say(number, **values):
    from oubi_web import text
    return text(number, **values)


def format_bytes(amount):
    from oubi_web import read_lang
    units = ("Б", "КБ", "МБ", "ГБ") if read_lang() == "ru" else ("B", "KB", "MB", "GB")
    value = float(max(0, amount))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == units[0]:
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} {units[-1]}"


def format_speed(amount):
    from oubi_web import read_lang
    suffix = "/с" if read_lang() == "ru" else "/s"
    return f"{format_bytes(amount)}{suffix}"


def progress_line(done, total, speed):
    if total > 0:
        shown = min(float(done), float(total))
        percent = shown / float(total) * 100
        body = say(42, percent=f"{percent:.0f}", done=format_bytes(shown), total=format_bytes(total))
    else:
        shown = float(done)
        body = say(43, done=format_bytes(shown))
    if speed is None:
        if total > 0 and shown >= float(total):
            return body + say(44)
        return body + say(45)
    return body + say(46, speed=format_speed(speed))


def model_status_line(cached, total):
    if total <= 0:
        return say(47)
    percent = min(100, float(cached) / float(total) * 100)
    return say(48, done=format_bytes(cached), total=format_bytes(total), percent=f"{percent:.0f}")


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
    print(say(49), flush=True)
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
        print(say(50), flush=True)
        return
    print(say(51, size=format_bytes(max(0, total - cached)), count=len(missing)), flush=True)
    meter = DownloadMeter(total=total, cached=cached)
    from huggingface_hub import hf_hub_download

    try:
        for name, size in missing:
            print(say(52, name=name, size=format_bytes(size)), flush=True)
            hf_hub_download(repo_id=repo_id, filename=name, tqdm_class=byte_progress(meter, name, size))
            print(say(53, name=name), flush=True)
    except Exception as error:
        if rate_limited(error):
            print(say(54), flush=True)
        raise
    print(say(55), flush=True)


def categories_of(result):
    return {name: sorted(result.get(name, {}), key=lambda tag: -result[name][tag]) for name in ORDER}


# ===== LOAD THE MODEL =====
def load_tagger(model, device="auto", architecture=None):
    import torch
    from transformers import pipeline

    if os.path.isdir(model):
        print(say(56), flush=True)
        source = model
    else:
        download_model(model)
        source = model
    print(say(57), flush=True)
    if device == "amd":
        index = amd_device(torch, architecture)
    elif device == "nvidia":
        index = nvidia_device(torch)
    else:
        index = 0 if device == "auto" and torch.cuda.is_available() else -1
    if index >= 0:
        print(say(58, name=torch.cuda.get_device_name(index)), flush=True)
    else:
        print(say(58, name="CPU") if device == "cpu" else say(59), flush=True)
    tagger = pipeline(
        model=source,
        image_processor=source,
        trust_remote_code=True,
        device=index,
        dtype=torch.float16 if index >= 0 else torch.float32,
    )
    print(say(60), flush=True)
    return tagger


# ===== TAG THE FOLDER =====
def find_pending_images(folders, exclude, force, report=None):
    last_report = float("-inf")

    def update(stage, done, total=None, *, immediate=False):
        nonlocal last_report
        if report is None:
            return
        now = time.monotonic()
        if immediate or now - last_report >= 0.5:
            report(stage, done, total)
            last_report = now

    update("discover", 0, immediate=True)
    files = set()
    for folder in folders:
        for path in image_files(folder, exclude):
            if path not in files:
                files.add(path)
                update("discover", len(files))
    ordered = sorted(files)
    total = len(ordered)
    if force or not total:
        return total, list(enumerate(ordered, 1))

    update("checking", 0, total, immediate=True)
    pending = []
    for index, path in enumerate(ordered, 1):
        if not sidecar_path(path).is_file():
            pending.append((index, path))
        update("checking", index, total, immediate=index == total)
    return total, pending


def main():
    from oubi_web import TAG_PREVIEW_EVENT, TAG_SCAN_EVENT

    report_preview = os.environ.get("OUBI_WEB_TAG_PREVIEW") == "1"

    def report_scan(stage, done, total):
        print(TAG_SCAN_EVENT + json.dumps([stage, done, total]), flush=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="+")
    ap.add_argument("--model", default="pixai-labs/pixai-tagger-v1.0")
    ap.add_argument("--csv", default="tags.csv")
    ap.add_argument("--no-txt", action="store_true", help="не писать .txt в подпапку tags")
    ap.add_argument("--exclude")
    ap.add_argument("--force", action="store_true", help="переразметить уже размеченные")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument("--device", choices=("auto", "cpu", "amd", "nvidia"), default="auto")
    ap.add_argument("--gpu-arch")
    ap.add_argument("--gpu-uuid")
    args = ap.parse_args()
    if args.batch_size < 1:
        print(say(61), file=sys.stderr)
        return 1

    if args.device == "amd":
        os.environ.pop("HSA_OVERRIDE_GFX_VERSION", None)
    elif args.device == "nvidia":
        if not args.gpu_uuid or not GPU_UUID.fullmatch(args.gpu_uuid):
            print(say(129), file=sys.stderr, flush=True)
            return 1
        os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu_uuid
    gfx = apply_gfx_override()
    if gfx:
        print("HSA_OVERRIDE_GFX_VERSION:", gfx)
    print(say(62, count=args.batch_size), flush=True)
    print(say(63), flush=True)

    try:
        tagger = load_tagger(args.model, args.device, args.gpu_arch)
    except GpuError as error:
        print(say(error.message_id), file=sys.stderr, flush=True)
        return 1
    from PIL import Image

    if not report_preview:
        print(say(64), flush=True)
    total, pending = find_pending_images(args.folder, args.exclude, args.force, report_scan if report_preview else None)
    print(say(65, total=total, done=total - len(pending), left=len(pending)), flush=True)
    if report_preview:
        report_scan(None, 0, None)
    if not pending:
        print(say(66), flush=True)

    csv_path = Path(args.csv)
    new_csv = not csv_path.exists()
    progress_root = os.environ.get("OUBI_PROGRESS_ROOT")
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
                    print(say(67, path=path, error=error), file=sys.stderr)
            if not loaded:
                continue
            if len(loaded) == 1:
                index, path, _image = loaded[0]
                display_path = ("/" + path.relative_to(progress_root).as_posix()
                                if progress_root and path.is_relative_to(progress_root) else path)
                print(say(68, path=display_path, index=index, total=total), flush=True)
            else:
                print(say(69, start=loaded[0][0], end=loaded[-1][0], total=total), flush=True)
            results = tag_images(tagger, [image for _, _, image in loaded])
            report_changes(path for (_, path, _), result in zip(loaded, results) if not isinstance(result, Exception))
            last_tagged = None
            for (index, path, image), result in zip(loaded, results):
                image.close()
                if isinstance(result, Exception):
                    print(say(67, path=path, error=result), file=sys.stderr)
                    continue
                cats = categories_of(result)
                if not args.no_txt:
                    sidecar = sidecar_path(path)
                    sidecar.parent.mkdir(exist_ok=True)
                    sidecar.write_text(
                        ", ".join(tag for name in ORDER for tag in cats[name]),
                        encoding="utf-8",
                    )
                writer.writerow([str(stored_image_path(path))] + [", ".join(cats[name]) for name in ORDER])
                last_tagged = path
                done += 1
                if done % 50 == 0:
                    handle.flush()
                elapsed = time.time() - started
                rate = done / elapsed if elapsed else 0
                left = (len(pending) - done) / rate / 3600 if rate else 0
                percent = done / len(pending) * 100 if pending else 100
                print(
                    say(
                        70,
                        percent=f"{percent:.0f}",
                        done=done,
                        total=len(pending),
                        rate=f"{rate:.2f}",
                        left=f"{left:.1f}",
                    ),
                    flush=True,
                )
            if report_preview and last_tagged is not None:
                handle.flush()
                print(TAG_PREVIEW_EVENT + json.dumps(str(last_tagged)), flush=True)
    print(say(71, count=done))
    return 0


if __name__ == "__main__":
    sys.exit(main())
