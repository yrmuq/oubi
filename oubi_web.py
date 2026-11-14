#!/usr/bin/env python3
"""Страница oubi: при старте только скачивает модель, разметку и раскладку запускают кнопки."""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ===== SETTINGS =====
HERE = Path(__file__).resolve().parent
IMAGES = "/images"
OUTPUT = "/output"
CSV_PATH = "/data/tags.csv"
GENDERS_PATH = "/data/genders.csv"
THEME_PATH = "/data/theme"
LAYOUT_PATH = "/data/layout.json"
GENRES_PATH = "/data/genres.txt"
MAX_GENRES = 20
THEMES = {"light", "dark", "auto"}
MODES = {"all", "tag", "sort", "plan"}
PAGE_PATH = HERE / "oubi_page.html"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
SORTED_LIST = "/data/sorted.txt"

def page_bytes(items, theme="auto", genres=None, layout=None):
    payload = json.dumps(items, ensure_ascii=False).replace("<", "\\u003c")
    chosen = theme if theme in THEMES else "auto"
    genre_payload = json.dumps(list(genres or [])[:MAX_GENRES], ensure_ascii=False).replace("<", "\\u003c")
    layout_payload = json.dumps(layout or {}, ensure_ascii=False).replace("<", "\\u003c")
    html = PAGE_PATH.read_text(encoding="utf-8")
    html = html.replace("FOLDERS_JSON", payload, 1).replace("__OUBI_THEME__", chosen, 1)
    html = html.replace("__OUBI_GENRES__", genre_payload, 1)
    html = html.replace("__OUBI_LAYOUT__", layout_payload, 1)
    return html.encode()


def read_genres(path=GENRES_PATH):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = None
    if lines is not None:
        picked = [line.strip()[:80] for line in lines[:MAX_GENRES]]
        while picked and not picked[-1]:
            picked.pop()
        if picked:
            return picked
    picked = [os.environ.get(f"OUBI_GENRE_{number}", "").strip() for number in range(1, MAX_GENRES + 1)]
    while picked and not picked[-1]:
        picked.pop()
    return picked


def write_genres(values, path=GENRES_PATH):
    if not isinstance(values, list) or len(values) > MAX_GENRES:
        return False
    cleaned = [str(value or "").strip().replace("\n", "")[:80] for value in values]
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("\n".join(cleaned) + ("\n" if cleaned else ""), encoding="utf-8")
    return True


def read_theme(path=THEME_PATH):
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return "auto"
    return value if value in THEMES else "auto"


def write_theme(value, path=THEME_PATH):
    if value not in THEMES:
        return False
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(value, encoding="utf-8")
    return True


LAYOUT_KEYS = ("left", "mid", "right", "preview", "genres", "info", "log")
BLOCK_NAMES = ("buttons", "folders", "progress", "preview", "genres", "info", "log")
PANEL_NAMES = ("left", "mid", "right")


def clean_blocks(value):
    if not isinstance(value, dict):
        return None
    seen = []
    clean = {}
    for panel in PANEL_NAMES:
        items = value.get(panel)
        if not isinstance(items, list):
            return None
        names = []
        for item in items:
            if not isinstance(item, str) or item not in BLOCK_NAMES or item in seen:
                return None
            seen.append(item)
            names.append(item)
        clean[panel] = names
    if len(seen) != len(BLOCK_NAMES):
        return None
    return clean


def read_layout(path=LAYOUT_PATH):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    clean = {}
    for key in LAYOUT_KEYS:
        value = data.get(key)
        if isinstance(value, (int, float)) and 40 <= float(value) <= 8000:
            clean[key] = int(value)
    blocks = clean_blocks(data.get("blocks"))
    if blocks:
        clean["blocks"] = blocks
    return clean


def write_layout(values, path=LAYOUT_PATH):
    if not isinstance(values, dict):
        return False
    current = read_layout(path)
    for key in LAYOUT_KEYS:
        if key not in values:
            continue
        value = values.get(key)
        if not isinstance(value, (int, float)) or not 40 <= float(value) <= 8000:
            return False
        current[key] = int(value)
    if "blocks" in values:
        blocks = clean_blocks(values.get("blocks"))
        if not blocks:
            return False
        current["blocks"] = blocks
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(current), encoding="utf-8")
    return True


def images_with_tag(tag, path=CSV_PATH, root=IMAGES):
    needle = (tag or "").strip().casefold()
    if not needle:
        return []
    base = Path(root).resolve()
    found = []
    seen = set()
    file = Path(path)
    columns = ("rating", "character", "copyright", "style", "general", "meta")
    if file.is_file():
        import csv
        csv.field_size_limit(10**9)
        try:
            handle = file.open(encoding="utf-8", errors="replace")
        except OSError:
            return []
        try:
            for row in csv.DictReader(handle):
                tags = []
                for key in columns:
                    tags.extend(part.strip() for part in (row.get(key) or "").split(",") if part.strip())
                if needle not in {item.casefold() for item in tags}:
                    continue
                raw = (row.get("file") or "").strip()
                if not raw:
                    continue
                image = Path(raw)
                if not image.is_absolute():
                    image = base / raw
                try:
                    rel = image.resolve().relative_to(base).as_posix()
                except (OSError, ValueError):
                    continue
                if rel in seen or not image.is_file():
                    continue
                seen.add(rel)
                found.append(rel)
        except (OSError, csv.Error):
            return found
        finally:
            handle.close()
        return found
    if not base.is_dir():
        return []
    for image in images_under(base):
        txt = image.with_suffix(".txt")
        if not txt.is_file():
            continue
        parts = [part.strip().casefold() for part in txt.read_text(encoding="utf-8", errors="replace").split(",")]
        if needle not in parts:
            continue
        found.append(image.resolve().relative_to(base).as_posix())
    return found


def mark(done, total):
    if total <= 0 or done <= 0:
        return "none"
    if done >= total:
        return "done"
    return "part"


def resolve_under(root, rel):
    base = Path(root).resolve()
    rel = (rel or "").strip().lstrip("/")
    path = base if rel in ("", ".") else (base / rel).resolve()
    if path != base and base not in path.parents:
        raise ValueError(rel)
    return path


def load_sorted_paths(path=SORTED_LIST):
    found = set()
    file = Path(path)
    if not file.is_file():
        return found
    for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            found.add(line)
    return found


def load_csv_paths(path=CSV_PATH):
    found = set()
    file = Path(path)
    if not file.is_file():
        return found
    import csv
    csv.field_size_limit(10**9)
    try:
        handle = file.open(encoding="utf-8", errors="replace")
    except OSError:
        return found
    try:
        for row in csv.DictReader(handle):
            name = (row.get("file") or "").strip()
            if name:
                found.add(name)
    except (OSError, csv.Error):
        return found
    finally:
        handle.close()
    return found


def folder_names(root=IMAGES):
    base = Path(root)
    if not base.is_dir():
        return []
    try:
        children = [path for path in base.iterdir() if path.is_dir() and not path.name.startswith(".")]
    except OSError as error:
        print(f"Сейчас: не читается {root}: {error}", flush=True)
        return []
    children.sort(key=lambda path: path.name.lower())
    if children:
        return [path.name for path in children]
    return [""]


def path_keys(path, root=None):
    keys = set()
    raw = str(path).strip()
    if not raw:
        return keys
    keys.add(raw)
    file = Path(raw)
    if root is not None and not file.is_absolute():
        file = Path(root) / raw
        keys.add(str(file))
    try:
        resolved = file.resolve()
    except OSError:
        return keys
    keys.add(str(resolved))
    if root is not None:
        try:
            keys.add(resolved.relative_to(Path(root).resolve()).as_posix())
        except ValueError:
            pass
    return keys


def index_paths(paths, root=None):
    found = set()
    for name in paths:
        found.update(path_keys(name, root))
    return found


def summarize_images(rel, images, csv_paths, sorted_paths, root=None):
    tagged = 0
    laid = 0
    tagged_keys = index_paths(csv_paths, root)
    sorted_keys = index_paths(sorted_paths, root)
    for image in images:
        try:
            keys = path_keys(image, root)
            txt = image.with_suffix(".txt")
            if keys & tagged_keys or (txt.is_file() and not txt.is_dir()):
                tagged += 1
            if keys & sorted_keys:
                laid += 1
        except OSError:
            continue
    total = len(images)
    return {
        "path": rel,
        "name": "Картинки" if rel == "" else Path(rel).name,
        "total": total,
        "tagged": tagged,
        "sorted": laid,
        "scan": mark(tagged, total),
        "sort": mark(laid, total),
    }


def images_under(folder):
    return [path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTS]


def source_folders(root=IMAGES):
    names = folder_names(root)
    if not names:
        return []
    try:
        csv_paths = load_csv_paths()
        sorted_paths = load_sorted_paths()
    except OSError:
        csv_paths, sorted_paths = set(), set()
    base = Path(root)
    found = []
    for name in names:
        folder = base if name == "" else base / name
        try:
            images = images_under(folder)
        except OSError:
            images = []
        found.append(summarize_images(name, images, csv_paths, sorted_paths, base))
    return found


def absolute_folders(rels, root=IMAGES):
    if not isinstance(rels, list) or not rels:
        return None, "Нужно выбрать папки."
    paths = []
    for rel in rels:
        if not isinstance(rel, str):
            return None, "Нет такой папки."
        try:
            path = resolve_under(root, rel)
        except ValueError:
            return None, "Нет такой папки."
        if not path.is_dir():
            return None, "Нет такой папки."
        paths.append(str(path))
    return paths, None


def content_type(path):
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }.get(Path(path).suffix.lower(), "application/octet-stream")


def browse_dir(root, rel):
    base = Path(root).resolve()
    path = resolve_under(base, rel)
    if not path.is_dir():
        raise ValueError(rel)
    items = []
    for child in path.iterdir():
        if child.name.startswith("."):
            continue
        items.append({
            "name": child.name,
            "dir": child.is_dir(),
            "path": str(child.relative_to(base)),
        })
    items.sort(key=lambda item: (not item["dir"], item["name"].lower()))
    parent = None if path == base else ("" if path.parent == base else str(path.parent.relative_to(base)))
    return {"path": "" if path == base else str(path.relative_to(base)), "parent": parent, "items": items}


def recent_tagged(rels, limit=40):
    base = Path(IMAGES).resolve()
    found = []
    for rel in rels:
        folder = resolve_under(base, rel)
        for image in images_under(folder):
            txt = image.with_suffix(".txt")
            if txt.is_file():
                found.append((txt.stat().st_mtime, str(image.resolve().relative_to(base))))
    found.sort()
    return [item for _, item in found[-limit:]]


def first_character(value):
    return (value or "").split(",")[0].strip()


def character_index(path=CSV_PATH):
    file = Path(path)
    if not file.is_file():
        return {}
    stat = file.stat()
    cached = getattr(character_index, "cache", None)
    if cached and cached["mtime"] == stat.st_mtime and cached["size"] == stat.st_size:
        return cached["rows"]
    import csv
    csv.field_size_limit(10**9)
    rows = {}
    try:
        handle = file.open(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    try:
        for row in csv.DictReader(handle):
            name = (row.get("file") or "").strip()
            if not name:
                continue
            character = first_character(row.get("character"))
            for key in path_keys(name, IMAGES):
                rows[key] = character
    except (OSError, csv.Error):
        return {}
    finally:
        handle.close()
    character_index.cache = {"mtime": stat.st_mtime, "size": stat.st_size, "rows": rows}
    return rows


def read_info(rel):
    base = Path(IMAGES).resolve()
    image = resolve_under(base, rel)
    text = ""
    txt = image.with_suffix(".txt")
    if txt.is_file() and (base in txt.resolve().parents or txt.resolve() == base):
        text = txt.read_text(encoding="utf-8", errors="replace")[:4000]
    names = character_index()
    character = ""
    for key in path_keys(image, base):
        if names.get(key):
            character = names[key]
            break
    return {"text": text, "character": character}


def parse_progress(line):
    text = line.strip()
    found = {}
    download = re.search(r"Скачивание:\s+(\d+)%", text)
    if download:
        found["percent"] = int(download.group(1))
        found["detail"] = text
        found["kind"] = "download"
        return found
    tagging = re.search(r"Разметка:\s+(\d+)%", text)
    if tagging:
        found["percent"] = int(tagging.group(1))
        found["detail"] = text
        found["kind"] = "tag"
        return found
    sorting = re.search(r"Раскладка:\s+(\d+)\s+из\s+(\d+)", text)
    if sorting:
        done, total = int(sorting.group(1)), int(sorting.group(2))
        found["percent"] = int(done * 100 / total) if total else 0
        found["detail"] = text
        found["kind"] = "sort"
        return found
    if text.startswith("Сейчас:"):
        found["step"] = text
    elif text.startswith(("Модель скачана", "Модель уже скачана", "Модель загружена", "Готово", "Итого:")):
        found["step"] = text
        if text.startswith(("Готово", "Итого:")):
            found["percent"] = 100
    return found


def flag_on(value):
    return str(value or "").strip() in {"1", "true", "TRUE", "yes", "YES"}


def reject_reason(model_state, job_running, mode):
    if mode not in MODES:
        return "Неизвестная кнопка."
    if job_running:
        return "Уже идёт другая работа."
    if mode in {"tag", "all"} and model_state != "ready":
        return "Сначала должна скачаться модель."
    return None


def preflight(mode):
    if mode in {"tag", "all"} and not os.path.isdir(IMAGES):
        return "Папка картинок не смонтирована в /images."
    batch = os.environ.get("OUBI_BATCH_SIZE", "1")
    if mode in {"tag", "all"} and (not batch.isdigit() or int(batch) < 1):
        return "OUBI_BATCH_SIZE должен быть 1 или больше."
    if mode in {"sort", "plan", "all"} and not os.path.isdir(OUTPUT):
        return "Папка результата не смонтирована в /output."
    if mode in {"sort", "plan"} and not os.path.isfile(CSV_PATH):
        return "Нет tags.csv. Сначала разметка."
    if mode in {"sort", "plan", "all"} and os.environ.get("OUBI_NSFW_FROM", "q") not in {"q", "e"}:
        return "OUBI_NSFW_FROM должен быть q или e."
    return None


# ===== COMMANDS =====
def tag_command(folders):
    command = [
        sys.executable,
        str(HERE / "tag_folder.py"),
        *folders,
        "--model",
        os.environ.get("OUBI_MODEL", "pixai-labs/pixai-tagger-v1.0"),
        "--csv",
        CSV_PATH,
        "--batch-size",
        os.environ.get("OUBI_BATCH_SIZE", "1"),
    ]
    if flag_on(os.environ.get("OUBI_NO_TXT")):
        command.append("--no-txt")
    if flag_on(os.environ.get("OUBI_FORCE")):
        command.append("--force")
    return command


def sort_command(plan=False, folders=None):
    command = [
        sys.executable,
        str(HERE / "sort_images.py"),
        "--csv",
        CSV_PATH,
        "--out",
        OUTPUT,
        "--nsfw-from",
        os.environ.get("OUBI_NSFW_FROM", "q"),
    ]
    if os.path.isfile(GENDERS_PATH):
        command.extend(["--genders", GENDERS_PATH])
    if folders:
        command.append("--only")
        command.extend(folders)
    if flag_on(os.environ.get("OUBI_MOVE")):
        command.append("--move")
    if plan:
        command.append("--dry-run")
    elif folders:
        command.extend(["--sorted-list", SORTED_LIST])
    if flag_on(os.environ.get("OUBI_COPY_TXT")):
        command.append("--copy-txt")
    genres = [item for item in read_genres() if item]
    if genres:
        command.extend(["--genres", ",".join(genres)])
    return command


class LineTee:
    def __init__(self, app, underlying):
        self.app = app
        self.underlying = underlying
        self.pending = ""

    def write(self, text):
        self.underlying.write(text)
        self.underlying.flush()
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.app.note(line, echo=False)
        return len(text)

    def flush(self):
        self.underlying.flush()


# ===== STATE =====
class App:
    def __init__(self):
        self.lock = threading.Lock()
        self.lines = []
        self.model_state = "downloading"
        self.job_running = False
        self.job_mode = None
        self.phase = "download"
        self.step = "Сейчас: скачиваю модель."
        self.detail = ""
        self.percent = None
        self.proc = None
        self.preview_file = ""
        self.current_file = ""
        self.folder_cache = None
        self.folder_cached_at = 0.0
        self.folder_counting = False

    def note(self, line, echo=True):
        text = line.strip()
        if not text:
            return
        with self.lock:
            self.lines.append(text)
            del self.lines[:-300]
            parsed = parse_progress(text)
            if "percent" in parsed:
                self.percent = parsed["percent"]
            if "detail" in parsed:
                self.detail = parsed["detail"]
            if "step" in parsed:
                self.step = parsed["step"]
            if "kind" in parsed:
                self.phase = parsed["kind"]
            found = re.search(r"Сейчас: размечаю (.+) \(\d+ из \d+\)\.?$", text)
            if found:
                path = found.group(1).strip()
                if self.current_file:
                    self.preview_file = self.current_file
                self.current_file = path
                if not self.preview_file:
                    self.preview_file = path
            elif text.startswith("Готово") and self.current_file:
                self.preview_file = self.current_file
        if echo:
            print(text, flush=True)

    def folders(self):
        self._ensure_count()
        with self.lock:
            cached = self.folder_cache
        if cached is not None:
            return cached
        return [
            {
                "path": name,
                "name": "Картинки" if name == "" else name,
                "total": 0,
                "tagged": 0,
                "sorted": 0,
                "scan": "none",
                "sort": "none",
            }
            for name in folder_names()
        ]

    def _ensure_count(self):
        with self.lock:
            fresh = self.folder_cache is not None and time.monotonic() - self.folder_cached_at < 8
            if fresh:
                return
            start = not self.folder_counting
            if start:
                self.folder_counting = True
        if start:
            self._count_folders()
            return
        while True:
            with self.lock:
                if not self.folder_counting:
                    return
            time.sleep(0.05)

    def _count_folders(self):
        try:
            found = source_folders()
        except Exception as error:
            print(f"Сейчас: не удалось посчитать папки: {error}", flush=True)
            found = None
        with self.lock:
            if found is not None:
                self.folder_cache = found
                self.folder_cached_at = time.monotonic()
            self.folder_counting = False

    def status(self):
        with self.lock:
            preview = self.preview_file
            payload = {
                "model": self.model_state,
                "job_running": self.job_running,
                "job_mode": self.job_mode,
                "phase": self.phase,
                "step": self.step,
                "detail": self.detail,
                "percent": self.percent,
                "preview_file": "",
                "log": self.lines[-80:],
            }
        if preview:
            try:
                payload["preview_file"] = str(Path(preview).resolve().relative_to(Path(IMAGES).resolve()))
            except ValueError:
                payload["preview_file"] = ""
        return payload

    def download(self):
        with self.lock:
            self.model_state = "downloading"
            self.phase = "download"
            self.percent = None
            self.step = "Сейчас: скачиваю модель."
        model = os.environ.get("OUBI_MODEL", "pixai-labs/pixai-tagger-v1.0")
        try:
            if os.path.isdir(model):
                self.note("Сейчас: модель локальная, скачивание не нужно.")
            else:
                from tag_folder import download_model

                previous = sys.stdout
                sys.stdout = LineTee(self, previous)
                try:
                    download_model(model)
                finally:
                    sys.stdout = previous
            with self.lock:
                self.model_state = "ready"
                self.step = "Модель скачана. Разметку и раскладку запускают кнопки."
                self.percent = 100
        except Exception as error:
            self.note(f"Сейчас: не удалось скачать модель. {error}")
            with self.lock:
                self.model_state = "error"

    def retry_download(self):
        with self.lock:
            if self.model_state == "downloading" or self.job_running:
                return False
            self.model_state = "downloading"
        threading.Thread(target=self.download, daemon=True).start()
        return True

    def start(self, mode, folders):
        paths, error = absolute_folders(folders)
        reason = error or preflight(mode)
        if reason:
            self.note(f"Сейчас: {reason}")
            return False
        with self.lock:
            if self.job_running:
                return False
            self.job_running = True
            self.job_mode = mode
            self.percent = None
            self.detail = ""
            self.phase = "sort" if mode in {"sort", "plan"} else "tag"
            self.step = "Сейчас: запускаю."
            self.folder_cache = None
        threading.Thread(target=self._run, args=(mode, paths), daemon=True).start()
        return True

    def _run(self, mode, folders):
        commands = []
        if mode in {"tag", "all"}:
            commands.append(tag_command(folders))
        if mode in {"sort", "plan", "all"}:
            commands.append(sort_command(plan=mode == "plan", folders=folders))
        code = 0
        try:
            for command in commands:
                if not self.job_running:
                    code = -15
                    break
                with self.lock:
                    if not self.job_running:
                        code = -15
                        break
                self.proc = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                for line in self.proc.stdout:
                    self.note(line)
                code = self.proc.wait()
                self.proc = None
                if code != 0:
                    break
        finally:
            with self.lock:
                self.job_running = False
                self.job_mode = None
                self.proc = None
                self.folder_cache = None
            if code == 0:
                self.note("Сейчас: работа закончилась.")
            elif code < 0:
                self.note("Сейчас: работу остановили.")
            else:
                self.note(f"Сейчас: работа завершилась с ошибкой {code}.")

    def stop(self):
        with self.lock:
            proc = self.proc
            self.job_running = False
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return


class Handler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, fmt, *args):
        return

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path
        if path == "/":
            self._send(200, page_bytes(self.app.folders(), read_theme(), read_genres(), read_layout()), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._send(200, self.app.status())
            return
        if path == "/api/folders":
            self._send(200, self.app.folders())
            return
        if path == "/api/browse":
            root = OUTPUT if query.get("root", ["output"])[0] == "output" else IMAGES
            try:
                self._send(200, browse_dir(root, query.get("path", [""])[0]))
            except ValueError:
                self._send(404, {"error": "нет такой папки"})
            return
        if path == "/api/recent":
            try:
                self._send(200, {"files": recent_tagged(query.get("folder", []))})
            except ValueError:
                self._send(404, {"error": "нет такой папки"})
            return
        if path == "/api/tagged":
            self._send(200, {"files": images_with_tag(query.get("tag", [""])[0])})
            return
        if path == "/api/tags":
            try:
                self._send(200, read_info(query.get("path", [""])[0]))
            except ValueError:
                self._send(404, {"error": "нет файла"})
            return
        if path == "/api/file":
            root = OUTPUT if query.get("root", ["images"])[0] == "output" else IMAGES
            try:
                file = resolve_under(root, query.get("path", [""])[0])
            except ValueError:
                self._send(404, {"error": "нет файла"})
                return
            if not file.is_file() or file.suffix.lower() not in IMAGE_EXTS:
                self._send(404, {"error": "нет файла"})
                return
            self._send(200, file.read_bytes(), content_type(file))
            return
        self._send(404, {"error": "нет такой страницы"})

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(min(length, 200000)) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "непонятный запрос"})
            return
        if parsed.path == "/api/theme":
            if not write_theme(payload.get("theme")):
                self._send(400, {"error": "нет такой темы"})
                return
            self._send(200, {"theme": read_theme()})
            return
        if parsed.path == "/api/layout":
            if not write_layout(payload if isinstance(payload, dict) else {}):
                self._send(400, {"error": "непонятные размеры"})
                return
            self._send(200, read_layout())
            return
        if parsed.path == "/api/genres":
            if not write_genres(payload.get("genres")):
                self._send(400, {"error": "непонятные жанры"})
                return
            self._send(200, {"genres": read_genres()})
            return
        if parsed.path == "/api/stop":
            self.app.stop()
            self._send(200, {"ok": True})
            return
        if parsed.path == "/api/download":
            if not self.app.retry_download():
                self._send(409, {"error": "скачивание уже идёт"})
                return
            self._send(200, {"ok": True})
            return
        if parsed.path != "/api/job":
            self._send(404, {"error": "нет такой страницы"})
            return
        mode = payload.get("mode")
        state = self.app.status()
        reason = reject_reason(state["model"], state["job_running"], mode)
        if reason:
            self._send(409, {"error": reason})
            return
        if not self.app.start(mode, payload.get("folders") or []):
            self._send(409, {"error": self.app.status()["step"]})
            return
        self._send(200, {"ok": True})


def main():
    app = App()
    Handler.app = app
    port = int(os.environ.get("OUBI_LISTEN", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=app.download, daemon=True).start()
    threading.Thread(target=app._ensure_count, daemon=True).start()
    print(f"Сейчас: страница на порту {port}.", flush=True)
    names = folder_names()
    if not names:
        print("Сейчас: в /images папок не видно.", flush=True)
    elif names == [""]:
        print("Сейчас: в /images нет вложенных папок, слева одна общая.", flush=True)
    else:
        print(f"Сейчас: в /images папок {len(names)}.", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
