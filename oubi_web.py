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
from string import Formatter
from urllib.parse import parse_qs, urlparse

from oubi_rocm import RocmError, RocmRuntime

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
LANG_PATH = "/data/language"
LANG_FILES = {"en": "English.ini", "ru": "Russian.ini"}
LANG_CHOICES = {"auto", *LANG_FILES}

def page_bytes(items, theme="auto", genres=None, layout=None, lang="en", catalogs=None, language_choice="auto"):
    payload = json.dumps(items, ensure_ascii=False).replace("<", "\\u003c")
    chosen = theme if theme in THEMES else "auto"
    genre_payload = json.dumps(list(genres or [])[:MAX_GENRES], ensure_ascii=False).replace("<", "\\u003c")
    layout_payload = json.dumps(layout or {}, ensure_ascii=False).replace("<", "\\u003c")
    lang_code = lang if lang in LANG_FILES else "en"
    language_choice = language_choice if language_choice in LANG_CHOICES else "auto"
    lang_payload = json.dumps(catalogs or {}, ensure_ascii=False).replace("<", "\\u003c")
    html = PAGE_PATH.read_text(encoding="utf-8")
    html = html.replace("FOLDERS_JSON", payload, 1).replace("__OUBI_THEME__", chosen, 1)
    html = html.replace("__OUBI_GENRES__", genre_payload, 1)
    html = html.replace("__OUBI_MAX_GENRES__", str(MAX_GENRES), 1)
    html = html.replace("__OUBI_LAYOUT__", layout_payload, 1)
    html = html.replace("__OUBI_LANG__", lang_code)
    html = html.replace("__OUBI_LANGUAGE_CHOICE__", language_choice, 1)
    html = html.replace("__OUBI_LANGS__", lang_payload, 1)
    return html.encode()


def parse_catalog(text):
    code = "en"
    rows = {}
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(";"):
            meta = line[1:].strip()
            if meta.lower().startswith("code") and "=" in meta:
                code = meta.split("=", 1)[1].strip().lower()
            continue
        parts = [part.strip() for part in line.split(" = ", 2)]
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        rows[int(parts[0])] = (parts[1], parts[2])
    return code, rows


def placeholder_fields(template):
    fields = []
    formatter = Formatter()

    def collect(value, depth=0):
        for _, field, spec, conversion in formatter.parse(value):
            if field is None:
                continue
            if depth > 1 or conversion not in {None, "s", "r", "a"}:
                raise ValueError("Invalid format field")
            fields.append((field, spec, conversion or ""))
            if spec:
                collect(spec, depth + 1)

    try:
        collect(template)
    except ValueError:
        return None
    numbered = [item for item in fields if not item[0].split(".")[0].split("[")[0]]
    return tuple(sorted(fields)), tuple(numbered)


def load_catalogs(root=None):
    base = (Path(root) if root else HERE / "languages").resolve()
    catalogs = {}
    english = {}
    for code, name in LANG_FILES.items():
        try:
            file = (base / name).resolve()
            file.relative_to(base)
            declared_code, rows = parse_catalog(file.read_text(encoding="utf-8-sig"))
            if declared_code != code:
                rows = {}
        except (OSError, ValueError):
            rows = {}
        if code == "en":
            english = rows
        packed = {}
        for number, (original, _) in english.items():
            translated = rows.get(number, ("", ""))[1]
            fields = placeholder_fields(translated)
            if not translated or fields is None or fields != placeholder_fields(original):
                translated = original
            packed[str(number)] = translated
        catalogs[code] = packed
    return catalogs


def language_code(value):
    token = re.split(r"[-_.@]", (value or "").strip().lower(), maxsplit=1)[0]
    return token if token in LANG_FILES else ""


def detect_lang(header="", environ=None):
    preferred = []
    for part in header.split(","):
        tag, _, weight = part.strip().partition(";")
        try:
            quality = float(weight.strip().removeprefix("q=")) if weight else 1
        except ValueError:
            continue
        if 0 < quality <= 1 and (found := language_code(tag)):
            preferred.append((quality, found))
    if preferred:
        return max(preferred, key=lambda item: item[0])[1]
    env = environ if environ is not None else os.environ
    for key in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for value in env.get(key, "").split(":"):
            if found := language_code(value):
                return found
    return "en"


def read_language_choice(path=None):
    path = LANG_PATH if path is None else path
    try:
        saved = Path(path).read_text(encoding="utf-8").strip().lower()
    except OSError:
        saved = ""
    return saved if saved in LANG_CHOICES else "auto"


def read_lang(path=None, header=""):
    saved = read_language_choice(path)
    return detect_lang(header) if saved == "auto" else saved


def write_lang(value, path=None):
    if not isinstance(value, str) or value not in LANG_CHOICES:
        return False
    file = Path(LANG_PATH if path is None else path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(value, encoding="utf-8")
    return True


def text(number, *, language=None, **values):
    packs = load_catalogs()
    pack = packs.get(language or os.environ.get("OUBI_JOB_LANGUAGE") or read_lang()) or packs.get("en") or {}
    template = pack.get(str(number)) or (packs.get("en") or {}).get(str(number)) or ""
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        english = (packs.get("en") or {}).get(str(number)) or ""
        try:
            return english.format(**values)
        except (KeyError, IndexError, ValueError):
            return english


def read_genres(path=GENRES_PATH):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = None
    if lines is not None:
        picked = [line.strip()[:80] for line in lines[:MAX_GENRES]]
        while picked and not picked[-1]:
            picked.pop()
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
    if not isinstance(value, str) or value not in THEMES:
        return False
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(value, encoding="utf-8")
    return True


LAYOUT_KEYS = ("left", "mid", "right", "preview", "genres", "info", "log")
BLOCK_NAMES = ("buttons", "folders", "progress", "preview", "genres", "info", "filter", "log")
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
    if "filter" not in seen and len(seen) == len(BLOCK_NAMES) - 1:
        placed = False
        for panel in PANEL_NAMES:
            if "info" in clean[panel]:
                clean[panel].insert(clean[panel].index("info") + 1, "filter")
                placed = True
                break
        if not placed:
            clean["right"].append("filter")
        seen.append("filter")
    if len(seen) != len(BLOCK_NAMES):
        return None
    return clean


def clean_heights(value):
    if not isinstance(value, dict):
        return None
    clean = {}
    for key, number in value.items():
        if key not in BLOCK_NAMES:
            continue
        if isinstance(number, (int, float)) and 24 <= float(number) <= 8000:
            clean[key] = int(number)
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
    heights = clean_heights(data.get("heights")) if isinstance(data.get("heights"), dict) else None
    if heights:
        clean["heights"] = heights
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
    if "heights" in values:
        heights = clean_heights(values.get("heights"))
        if heights is None:
            return False
        current["heights"] = heights
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(current), encoding="utf-8")
    return True


def images_with_tag(tag, path=CSV_PATH, root=IMAGES):
    return images_with_tags([tag], path, root)


def images_with_tags(tags, path=CSV_PATH, root=IMAGES):
    required = []
    seen_needles = set()
    for tag in tags or []:
        folded = str(tag or "").strip().casefold()
        if not folded or folded in seen_needles:
            continue
        seen_needles.add(folded)
        required.append(folded)
    if not required:
        return []
    needed = set(required)
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
                if not needed <= {item.casefold() for item in tags}:
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
        if not needed <= set(parts):
            continue
        found.append(image.resolve().relative_to(base).as_posix())
    return found


def tags_in_folders(rels, path=CSV_PATH, root=IMAGES):
    base = Path(root).resolve()
    folder_rels = []
    for rel in rels or []:
        folder = resolve_under(base, rel)
        if not folder.is_dir():
            raise ValueError(rel)
        folder_rels.append("" if folder == base else folder.relative_to(base).as_posix())
    file = Path(path)
    stamp = (file.stat().st_mtime_ns, file.stat().st_size) if file.is_file() else (0, 0)
    key = (str(file), stamp, tuple(sorted(folder_rels)), str(base))
    cached = getattr(tags_in_folders, "cache", None)
    if cached and cached["key"] == key:
        return cached["tags"]
    seen = set()
    found = []

    def add(raw):
        token = str(raw or "").strip()
        folded = token.casefold()
        if not token or folded in seen:
            return
        seen.add(folded)
        found.append(token)

    def listed(rel):
        rel = rel.strip("/")
        for folder in folder_rels:
            if folder == "" or rel == folder or rel.startswith(folder + "/"):
                return True
        return False

    def relative_file(raw):
        text = str(raw or "").strip().replace("\\", "/")
        if not text:
            return ""
        image = Path(text)
        if image.is_absolute():
            prefix = base.as_posix().rstrip("/") + "/"
            full = image.as_posix()
            if full == base.as_posix():
                return ""
            if not full.startswith(prefix):
                return ""
            return full[len(prefix):]
        return image.as_posix().lstrip("/")

    columns = ("rating", "character", "copyright", "style", "general", "meta")
    if file.is_file():
        import csv
        csv.field_size_limit(10**9)
        try:
            handle = file.open(encoding="utf-8", errors="replace")
        except OSError:
            handle = None
        if handle is not None:
            try:
                for row in csv.DictReader(handle):
                    if not listed(relative_file(row.get("file"))):
                        continue
                    for column in columns:
                        for part in (row.get(column) or "").split(","):
                            add(part)
            except (OSError, csv.Error):
                pass
            finally:
                handle.close()
    else:
        for rel in folder_rels:
            folder = base if rel == "" else base / rel
            try:
                images = images_under(folder)
            except OSError:
                images = []
            for image in images:
                txt = image.with_suffix(".txt")
                if not txt.is_file():
                    continue
                try:
                    raw = txt.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for part in raw.split(","):
                    add(part)
    found.sort(key=str.casefold)
    tags_in_folders.cache = {"key": key, "tags": found}
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
        print(text(26, path=root, error=error), flush=True)
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
        "name": text(84) if rel == "" else Path(rel).name,
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


def absolute_folders(rels, root=IMAGES, *, language=None):
    if not isinstance(rels, list) or not rels:
        return None, text(85, language=language)
    paths = []
    for rel in rels:
        if not isinstance(rel, str):
            return None, text(86, language=language)
        try:
            path = resolve_under(root, rel)
        except ValueError:
            return None, text(86, language=language)
        if not path.is_dir():
            return None, text(86, language=language)
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
    download = re.search(r"(?:Скачивание|Download):\s+(\d+)%", text)
    if download:
        found["percent"] = int(download.group(1))
        found["detail"] = text
        found["kind"] = "download"
        return found
    tagging = re.search(r"(?:Разметка|Tagging):\s+(\d+)%", text)
    if tagging:
        found["percent"] = int(tagging.group(1))
        found["detail"] = text
        found["kind"] = "tag"
        return found
    sorting = re.search(r"(?:Раскладка|Sorting):\s+(\d+)\s+(?:из|of)\s+(\d+)", text)
    if sorting:
        done, total = int(sorting.group(1)), int(sorting.group(2))
        found["percent"] = int(done * 100 / total) if total else 0
        found["detail"] = text
        found["kind"] = "sort"
        return found
    if text.startswith(("Сейчас:", "Now:")):
        found["step"] = text
    elif text.startswith((
        "Модель скачана", "Модель уже скачана", "Модель загружена", "Готово", "Итого:",
        "Model downloaded", "Model already downloaded", "Model loaded", "Done", "Result:",
    )):
        found["step"] = text
        if text.startswith(("Готово", "Итого:", "Done", "Result:")):
            found["percent"] = 100
    return found


def flag_on(value):
    return str(value or "").strip() in {"1", "true", "TRUE", "yes", "YES"}


def reject_reason(model_state, job_running, mode, *, language=None):
    if not isinstance(mode, str) or mode not in MODES:
        return text(32, language=language)
    if job_running:
        return text(33, language=language)
    if mode in {"tag", "all"} and model_state != "ready":
        return text(34, language=language)
    return None


def preflight(mode, *, language=None):
    if mode in {"tag", "all"} and not os.path.isdir(IMAGES):
        return text(35, language=language)
    batch = os.environ.get("OUBI_BATCH_SIZE", "1")
    if mode in {"tag", "all"} and (not batch.isdigit() or int(batch) < 1):
        return text(36, language=language)
    if mode in {"sort", "plan", "all"} and not os.path.isdir(OUTPUT):
        return text(37, language=language)
    if mode in {"sort", "plan"} and not os.path.isfile(CSV_PATH):
        return text(38, language=language)
    if mode in {"sort", "plan", "all"} and os.environ.get("OUBI_NSFW_FROM", "q") not in {"q", "e"}:
        return text(39, language=language)
    return None


# ===== COMMANDS =====
def tag_command(folders, *, device="cpu", architecture=None, python=None):
    command = [
        python or sys.executable,
        str(HERE / "tag_folder.py"),
        *folders,
        "--model",
        os.environ.get("OUBI_MODEL", "pixai-labs/pixai-tagger-v1.0"),
        "--csv",
        CSV_PATH,
        "--batch-size",
        os.environ.get("OUBI_BATCH_SIZE", "1"),
        "--device",
        device,
    ]
    if architecture:
        command.extend(["--gpu-arch", architecture])
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
        self.rocm = RocmRuntime()
        self.lines = []
        self.model_state = "downloading"
        self.job_running = False
        self.job_mode = None
        self.phase = "download"
        self.step = text(22)
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
            found = re.search(r"(?:Сейчас: размечаю|Now: tagging) (.+) \(\d+ (?:из|of) \d+\)\.?$", text)
            if found:
                path = found.group(1).strip()
                if self.current_file:
                    self.preview_file = self.current_file
                self.current_file = path
                if not self.preview_file:
                    self.preview_file = path
            elif text.startswith(("Готово", "Done")) and self.current_file:
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
                "name": text(84) if name == "" else name,
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
            print(text(27, error=error), flush=True)
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
            self.step = text(22)
        model = os.environ.get("OUBI_MODEL", "pixai-labs/pixai-tagger-v1.0")
        try:
            if os.path.isdir(model):
                self.note(text(56))
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
                self.step = text(23)
                self.percent = 100
        except Exception as error:
            self.note(text(24, error=error))
            with self.lock:
                self.model_state = "error"

    def retry_download(self):
        with self.lock:
            if self.model_state == "downloading" or self.job_running:
                return False
            self.model_state = "downloading"
        threading.Thread(target=self.download, daemon=True).start()
        return True

    def start(self, mode, folders, *, language=None, device="cpu", architecture="auto"):
        paths, error = absolute_folders(folders, language=language)
        reason = error or preflight(mode, language=language)
        if reason:
            self.note(text(79, reason=reason, language=language))
            return reason
        tagging = None
        if mode in {"tag", "all"}:
            if device not in ("cpu", "amd"):
                return text(122, language=language)
            try:
                arch = self.rocm.resolve(architecture) if device == "amd" else None
                python = self.rocm.python_for(arch) if arch else None
            except RocmError as error:
                return text(error.message_id, language=language)
            tagging = tag_command(paths, device=device, architecture=arch, python=python)
        with self.lock:
            if self.job_running:
                return text(33, language=language)
            self.job_running = True
            self.job_mode = mode
            self.percent = None
            self.detail = ""
            self.phase = "sort" if mode in {"sort", "plan"} else "tag"
            self.step = text(25, language=language)
            self.folder_cache = None
        threading.Thread(target=self._run, args=(mode, paths, language, tagging), daemon=True).start()
        return None

    def _run(self, mode, folders, language, tagging):
        commands = [tagging] if tagging else []
        if mode in {"sort", "plan", "all"}:
            commands.append(sort_command(plan=mode == "plan", folders=folders))
        code = 0
        env = dict(os.environ, OUBI_JOB_LANGUAGE=language or read_lang(), PYTHONNOUSERSITE="1")
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
                    env=env,
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
        except OSError as error:
            code = 1
            self.note(text(26, path=command[0], error=error, language=language))
        finally:
            with self.lock:
                self.job_running = False
                self.job_mode = None
                self.proc = None
                self.folder_cache = None
            if code == 0:
                self.note(text(80, language=language))
            elif code < 0:
                self.note(text(81, language=language))
            else:
                self.note(text(82, code=code, language=language))

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

    def _language(self):
        return read_lang(header=self.headers.get("Accept-Language", ""))

    def _error(self, status, message):
        self._send(status, {"error": text(message, language=self._language())})

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path
        if path == "/":
            page = page_bytes(
                self.app.folders(), read_theme(), read_genres(), read_layout(),
                self._language(), load_catalogs(), read_language_choice(),
            )
            self._send(200, page, "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._send(200, {**self.app.status(), "rocm": self.app.rocm.status()})
            return
        if path == "/api/folders":
            self._send(200, self.app.folders())
            return
        if path == "/api/browse":
            root = OUTPUT if query.get("root", ["output"])[0] == "output" else IMAGES
            try:
                self._send(200, browse_dir(root, query.get("path", [""])[0]))
            except ValueError:
                self._error(404, 86)
            return
        if path == "/api/recent":
            try:
                self._send(200, {"files": recent_tagged(query.get("folder", []))})
            except ValueError:
                self._error(404, 86)
            return
        if path == "/api/tagged":
            self._send(200, {"files": images_with_tags(query.get("tag", []))})
            return
        if path == "/api/vocabulary":
            try:
                self._send(200, {"tags": tags_in_folders(query.get("folder", []))})
            except ValueError:
                self._error(404, 86)
            return
        if path == "/api/tags":
            try:
                self._send(200, read_info(query.get("path", [""])[0]))
            except ValueError:
                self._error(404, 87)
            return
        if path == "/api/file":
            root = OUTPUT if query.get("root", ["images"])[0] == "output" else IMAGES
            try:
                file = resolve_under(root, query.get("path", [""])[0])
            except ValueError:
                self._error(404, 87)
                return
            if not file.is_file() or file.suffix.lower() not in IMAGE_EXTS:
                self._error(404, 87)
                return
            self._send(200, file.read_bytes(), content_type(file))
            return
        self._error(404, 88)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if not 0 <= length <= 200000:
                raise ValueError("Invalid body length")
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
        except ValueError:
            self._error(400, 89)
            return
        if parsed.path == "/api/theme":
            if not write_theme(payload.get("theme")):
                self._error(400, 90)
                return
            self._send(200, {"theme": read_theme()})
            return
        if parsed.path == "/api/language":
            if not write_lang(payload.get("language")):
                self._error(400, 91)
                return
            self._send(200, {"choice": read_language_choice(), "language": self._language()})
            return
        if parsed.path == "/api/layout":
            if not write_layout(payload if isinstance(payload, dict) else {}):
                self._error(400, 92)
                return
            self._send(200, read_layout())
            return
        if parsed.path == "/api/genres":
            if not write_genres(payload.get("genres")):
                self._error(400, 93)
                return
            self._send(200, {"genres": read_genres()})
            return
        if parsed.path == "/api/stop":
            self.app.stop()
            self._send(200, {"ok": True})
            return
        if parsed.path == "/api/download":
            if not self.app.retry_download():
                self._error(409, 99)
                return
            self._send(200, {"ok": True})
            return
        if parsed.path == "/api/rocm":
            if self.headers.get_content_type() != "application/json":
                self._error(400, 89)
                return
            try:
                self.app.rocm.start(payload.get("architecture", "auto"))
            except RocmError as error:
                self._error(409, error.message_id)
                return
            self._send(202, self.app.rocm.status())
            return
        if parsed.path != "/api/job":
            self._error(404, 88)
            return
        mode = payload.get("mode")
        state = self.app.status()
        language = self._language()
        reason = reject_reason(state["model"], state["job_running"], mode, language=language)
        if reason:
            self._send(409, {"error": reason})
            return
        if reason := self.app.start(
            mode, payload.get("folders") or [], language=language,
            device=payload.get("device", "cpu"), architecture=payload.get("architecture", "auto"),
        ):
            self._send(409, {"error": reason})
            return
        self._send(200, {"ok": True})


def main():
    app = App()
    Handler.app = app
    port = int(os.environ.get("OUBI_LISTEN", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=app.download, daemon=True).start()
    threading.Thread(target=app._ensure_count, daemon=True).start()
    print(text(28, port=port), flush=True)
    names = folder_names()
    if not names:
        print(text(29), flush=True)
    elif names == [""]:
        print(text(30), flush=True)
    else:
        print(text(31, count=len(names)), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
