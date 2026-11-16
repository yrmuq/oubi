#!/usr/bin/env python3
"""Страница oubi: при старте только скачивает модель, разметку и раскладку запускают кнопки."""
import csv
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Formatter
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from oubi_cryptomator import COOKIE_NAME, Vault, VaultError
from oubi_files import IMAGE_EXTS, ImageChangedError, delete_source_image, image_files, image_revision, sidecar_path
from oubi_gpu import GpuError
from oubi_nvidia import NvidiaRuntime
from oubi_rocm import RocmRuntime

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
SORTED_LIST = "/data/sorted.txt"
LANG_PATH = "/data/language"
LANG_FILES = {"en": "English.ini", "ru": "Russian.ini"}
LANG_CHOICES = {"auto", *LANG_FILES}

@dataclass(frozen=True)
class Workspace:
    images: str
    output: str
    csv: str
    sorted_list: str
    genders: str
    excluded: str | None = None
    data: str | None = None

    @classmethod
    def encrypted(cls, root):
        relative = Path(os.environ.get("OUBI_CRYPT_OUTPUT", "Sorted"))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise VaultError(168)
        data = root / ".oubi"
        output = (root / relative).resolve()
        if (not output.is_relative_to(root) or output == root or output.is_relative_to(data)
                or data.is_relative_to(output) or "tags" in relative.parts or data.is_symlink()
                or (output.exists() and not output.is_dir())):
            raise VaultError(168)
        data.mkdir(mode=0o700, exist_ok=True)
        for name in ("tags.csv", "sorted.txt", "genders.csv", "unknown_genders.txt"):
            if (data / name).is_symlink():
                raise VaultError(168)
        return cls(str(root), str(output), str(data / "tags.csv"), str(data / "sorted.txt"),
                   str(data / "genders.csv"), str(output), str(data))


def normal_workspace():
    return Workspace(IMAGES, OUTPUT, CSV_PATH, SORTED_LIST, GENDERS_PATH)


def page_bytes(items, theme="auto", genres=None, layout=None, lang="en", catalogs=None, language_choice="auto",
               *, vault_enabled=False, vault_scope=""):
    payload = json.dumps(items, ensure_ascii=False).replace("<", "\\u003c")
    chosen = theme if theme in THEMES else "auto"
    genre_payload = json.dumps(list(genres or [])[:MAX_GENRES], ensure_ascii=False).replace("<", "\\u003c")
    layout_payload = json.dumps(layout or {}, ensure_ascii=False).replace("<", "\\u003c")
    lang_code = lang if lang in LANG_FILES else "en"
    language_choice = language_choice if language_choice in LANG_CHOICES else "auto"
    lang_payload = json.dumps(catalogs or {}, ensure_ascii=False).replace("<", "\\u003c")
    html = PAGE_PATH.read_text(encoding="utf-8")
    html = html.replace("__OUBI_VAULT_HIDDEN__", "" if vault_enabled else " hidden", 1)
    html = html.replace("__OUBI_VAULT_SCOPE__", json.dumps(vault_scope), 1)
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


def gpu_transfer_text(transfer, downloaded, expected, language):
    def mib(size):
        return f"{size / 1048576:.1f}"

    def eta(remaining):
        seconds = int(remaining / max(transfer["speed"], 1))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    remaining = max(0, transfer["total"] - transfer["received"])
    progress = text(155, language=language, file=transfer["file"], received=mib(transfer["received"]),
                    total=mib(transfer["total"]), speed=mib(transfer["speed"]),
                    remaining=mib(remaining), eta=eta(remaining))
    if expected > downloaded + remaining:
        progress += " " + text(156, language=language, remaining=mib(expected - downloaded),
                               eta=eta(expected - downloaded))
    return progress


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


def images_with_tags(tags, path=CSV_PATH, root=IMAGES, *, exclude=None):
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
                if rel in seen or not image.is_file() or in_output(image, exclude):
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
    for image in images_under(base, exclude):
        txt = sidecar_path(image)
        if not txt.is_file():
            continue
        parts = [part.strip().casefold() for part in txt.read_text(encoding="utf-8", errors="replace").split(",")]
        if not needed <= set(parts):
            continue
        found.append(image.resolve().relative_to(base).as_posix())
    return found


def tags_in_folders(rels, path=CSV_PATH, root=IMAGES, *, exclude=None):
    base = Path(root).resolve()
    folder_rels = []
    for rel in rels or []:
        folder = resolve_under(base, rel)
        if not folder.is_dir() or in_output(folder, exclude):
            raise ValueError(rel)
        folder_rels.append("" if folder == base else folder.relative_to(base).as_posix())
    file = Path(path)
    stamp = (file.stat().st_mtime_ns, file.stat().st_size) if file.is_file() else (0, 0)
    key = (str(file), stamp, tuple(sorted(folder_rels)), str(base))
    cached = getattr(tags_in_folders, "cache", None)
    if not exclude and cached and cached["key"] == key:
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
                    rel = relative_file(row.get("file"))
                    if not listed(rel) or in_output(base / rel, exclude):
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
                images = images_under(folder, exclude)
            except OSError:
                images = []
            for image in images:
                txt = sidecar_path(image)
                if not txt.is_file():
                    continue
                try:
                    raw = txt.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for part in raw.split(","):
                    add(part)
    found.sort(key=str.casefold)
    if not exclude:
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


def load_sorted_paths(path=SORTED_LIST, *, cancelled=None):
    found = set()
    file = Path(path)
    if not file.is_file():
        return found
    with file.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if cancelled and cancelled():
                break
            line = line.strip()
            if line:
                found.add(line)
    return found


def load_csv_paths(path=CSV_PATH, *, cancelled=None):
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
            if cancelled and cancelled():
                break
            name = (row.get("file") or "").strip()
            if name:
                found.add(name)
    except (OSError, csv.Error):
        return found
    finally:
        handle.close()
    return found


def folder_names(root=IMAGES, *, exclude=None):
    base = Path(root)
    if not base.is_dir():
        return []
    try:
        children = [path for path in base.iterdir() if path.is_dir() and path.name != "tags"
                    and not path.name.startswith(".") and not in_output(path, exclude)]
    except OSError as error:
        if not exclude:
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


def index_paths(paths, root=None, *, cancelled=None):
    found = set()
    for name in paths:
        if cancelled and cancelled():
            break
        found.update(path_keys(name, root))
    return found


def summarize_images(rel, images, csv_paths, sorted_paths, root=None, *, cancelled=None):
    return _summarize_images(rel, images, index_paths(csv_paths, root, cancelled=cancelled),
                             index_paths(sorted_paths, root, cancelled=cancelled), root, cancelled=cancelled)


def _summarize_images(rel, images, tagged_keys, sorted_keys, root, *, cancelled=None, checked=None):
    tagged = 0
    laid = 0
    for image in images:
        if cancelled and cancelled():
            break
        try:
            keys = path_keys(image, root)
            txt = sidecar_path(image)
            if keys & tagged_keys or (txt.is_file() and not txt.is_dir()):
                tagged += 1
            if keys & sorted_keys:
                laid += 1
        except OSError:
            pass
        if checked is not None:
            checked()
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


def in_output(path, exclude):
    return bool(exclude and Path(path).resolve().is_relative_to(exclude))


def images_under(folder, exclude=None, *, cancelled=None):
    return list(image_files(folder, exclude, cancelled=cancelled))


def folder_paths(root, directory, exclude):
    base = Path(root)
    folder = resolve_under(base, directory) if directory else base
    if directory:
        rel = folder.relative_to(base.resolve())
        if (not folder.is_dir() or in_output(folder, exclude)
                or any(part.startswith(".") or part == "tags" for part in rel.parts)):
            raise ValueError(directory)
        directory = rel.as_posix() if rel.parts else ""
    return ["/".join(part for part in (directory, name) if part)
            for name in folder_names(folder, exclude=exclude)]


def source_folders(root=IMAGES, *, directory="", csv=CSV_PATH, sorted_list=SORTED_LIST,
                   exclude=None, cancelled=None, progress=None):
    names = folder_paths(root, directory, exclude)
    if not names:
        if progress is not None and not (cancelled and cancelled()):
            progress("done", 0, 0)
        return []
    base = Path(root)
    total = 0

    def discover():
        nonlocal total
        for name in names:
            if cancelled and cancelled():
                return
            images = []
            try:
                for image in image_files(base / name, exclude, cancelled=cancelled):
                    images.append(image)
                    if progress is not None:
                        total += 1
                        progress("discover", total)
            except OSError:
                pass
            yield name, images

    candidates = discover()
    if progress is not None:
        candidates = list(candidates)
        if cancelled and cancelled():
            return []
        progress("checking", 0, total)

    try:
        csv_paths = load_csv_paths(csv, cancelled=cancelled)
        sorted_paths = load_sorted_paths(sorted_list, cancelled=cancelled)
    except OSError:
        csv_paths, sorted_paths = set(), set()
    tagged_keys = index_paths(csv_paths, base, cancelled=cancelled)
    sorted_keys = index_paths(sorted_paths, base, cancelled=cancelled)

    processed = 0

    def checked():
        nonlocal processed
        processed += 1
        progress("checking", processed, total)

    summaries = []
    for name, images in candidates:
        if cancelled and cancelled():
            return []
        summaries.append(_summarize_images(name, images, tagged_keys, sorted_keys, base, cancelled=cancelled,
                                           checked=checked if progress is not None else None))
    if not (cancelled and cancelled()) and progress is not None:
        progress("done", total, total)
    return summaries


def absolute_folders(rels, root=IMAGES, *, language=None, exclude=None):
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
        if not path.is_dir() or in_output(path, exclude):
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


def recent_tagged(rels, limit=40, *, root=IMAGES, exclude=None):
    base = Path(root).resolve()
    found = []
    for rel in rels:
        folder = resolve_under(base, rel)
        for image in images_under(folder, exclude):
            txt = sidecar_path(image)
            if txt.is_file():
                found.append((txt.stat().st_mtime, str(image.resolve().relative_to(base))))
    found.sort()
    return [item for _, item in found[-limit:]]


def first_character(value):
    return (value or "").split(",")[0].strip()


def character_index(path=CSV_PATH, root=IMAGES, *, cache=True):
    file = Path(path)
    if not file.is_file():
        return {}
    stat = file.stat()
    cached = getattr(character_index, "cache", None)
    if cache and cached and cached["key"] == (file, root, stat.st_mtime_ns, stat.st_size):
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
            for key in path_keys(name, root):
                rows[key] = character
    except (OSError, csv.Error):
        return {}
    finally:
        handle.close()
    if cache:
        character_index.cache = {"key": (file, root, stat.st_mtime_ns, stat.st_size), "rows": rows}
    return rows


def read_info(rel, *, root=IMAGES, csv=CSV_PATH, encrypted=False):
    base = Path(root).resolve()
    image = resolve_under(base, rel)
    text = ""
    txt = sidecar_path(image)
    if txt.is_file() and (base in txt.resolve().parents or txt.resolve() == base):
        text = txt.read_text(encoding="utf-8", errors="replace")[:4000]
    names = character_index(csv, root, cache=not encrypted)
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


def preflight(mode, *, language=None, workspace=None, output=None):
    workspace = workspace or normal_workspace()
    output = output or workspace.output
    if mode in {"tag", "all"} and not os.path.isdir(workspace.images):
        return text(35, language=language)
    batch = os.environ.get("OUBI_BATCH_SIZE", "1")
    if mode in {"tag", "all"} and (not batch.isdigit() or int(batch) < 1):
        return text(36, language=language)
    if mode in {"sort", "plan", "all"} and not os.path.isdir(output) and output != workspace.excluded:
        return text(37, language=language)
    if mode in {"sort", "plan"} and not os.path.isfile(workspace.csv):
        return text(38, language=language)
    if mode in {"sort", "plan", "all"} and os.environ.get("OUBI_NSFW_FROM", "q") not in {"q", "e"}:
        return text(39, language=language)
    return None


# ===== COMMANDS =====
def tag_command(folders, *, device="cpu", architecture=None, gpu=None, python=None, workspace=None):
    workspace = workspace or normal_workspace()
    command = [
        python or sys.executable,
        str(HERE / "tag_folder.py"),
        *folders,
        "--model",
        os.environ.get("OUBI_MODEL", "pixai-labs/pixai-tagger-v1.0"),
        "--csv",
        workspace.csv,
        "--batch-size",
        os.environ.get("OUBI_BATCH_SIZE", "1"),
        "--device",
        device,
    ]
    if workspace.excluded:
        command.extend(["--exclude", workspace.excluded])
    if architecture:
        command.extend(["--gpu-arch", architecture])
    if gpu:
        command.extend(["--gpu-uuid", gpu])
    if flag_on(os.environ.get("OUBI_NO_TXT")):
        command.append("--no-txt")
    if flag_on(os.environ.get("OUBI_FORCE")):
        command.append("--force")
    return command


def sort_command(plan=False, folders=None, *, workspace=None, output=None):
    workspace = workspace or normal_workspace()
    command = [
        sys.executable,
        str(HERE / "sort_images.py"),
        "--csv",
        workspace.csv,
        "--out",
        output or workspace.output,
        "--nsfw-from",
        os.environ.get("OUBI_NSFW_FROM", "q"),
    ]
    if workspace.excluded:
        command.extend(["--exclude", workspace.excluded])
    if os.path.isfile(workspace.genders):
        command.extend(["--genders", workspace.genders])
    if folders:
        command.append("--only")
        command.extend(folders)
    if flag_on(os.environ.get("OUBI_MOVE")):
        command.append("--move")
    if plan:
        command.append("--dry-run")
    elif folders:
        command.extend(["--sorted-list", workspace.sorted_list])
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
    def __init__(self, workspace=None, *, devices=None):
        self.workspace = workspace
        self.vault = None if workspace else Vault()
        self.lock = threading.Lock()
        self.gpu_lock = threading.Lock()
        self.rocm, self.nvidia = devices or (RocmRuntime(), NvidiaRuntime())
        self.gpu_prompt = None
        self.lines = []
        self.model_state = "downloading"
        self.job_running = False
        self.cancel_requested = False
        self.job_mode = None
        self.phase = "download"
        self.step = text(22)
        self.detail = ""
        self.percent = None
        self.proc = None
        self.preview_file = ""
        self.current_file = ""
        self.folder_cache = None
        self.folder_scan = None
        self.scan_progress = None

    def _append_line(self, line):
        self.lines.append(line)
        del self.lines[:-300]

    def note(self, line, echo=True):
        text = line.strip()
        if not text:
            return
        with self.lock:
            self._append_line(text)
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
        if echo and self.workspace is None:
            print(text, flush=True)

    def gpu_note(self, message_id, language, **values):
        line = text(message_id, language=language, **values)
        with self.lock:
            self._append_line(line)

    def request_gpu_install(self, vendor, choice, language):
        with self.gpu_lock:
            if self.gpu_prompt:
                raise GpuError(152)
            if self.gpu_busy():
                raise GpuError(120)
            runtime = self.rocm if vendor == "amd" else self.nvidia
            runtime.check_platform()
            if vendor == "amd":
                target, gpu = runtime.resolve(choice), None
                package = f"AMD ROCm {target}"
            else:
                target, gpu = runtime.resolve(choice)
                name = next((card["name"] for card in runtime.gpus() if card["uuid"] == gpu), gpu)
                package = f"{name} (CUDA {target})"
            if runtime.installed(target, gpu):
                raise GpuError(153)
            expected = runtime.download_size(target)
            size = f"{expected / 1e9:.2f}".rstrip("0").rstrip(".")
            message = text(141 if expected else 142, language=language, package=package, size=size)
            self.gpu_prompt = {"id": uuid4().hex, "vendor": vendor, "target": target, "gpu": gpu,
                               "message": message}
            with self.lock:
                self._append_line(message)

    def gpu_busy(self):
        return any(runtime.installation_status()["state"] in {"installing", "checking"}
                   for runtime in (self.rocm, self.nvidia))

    def answer_gpu_install(self, prompt_id, answer, language):
        if not isinstance(answer, str) or (answer := answer.strip().lower()) not in {"y", "n"}:
            raise GpuError(144)
        with self.gpu_lock:
            prompt = self.gpu_prompt
            if not prompt or prompt["id"] != prompt_id:
                raise GpuError(145)
            self.gpu_prompt = None
            with self.lock:
                self._append_line(f"> {answer}")
            if answer == "n":
                self.gpu_note(143, language)
                return
            vendor, target, gpu = prompt["vendor"], prompt["target"], prompt["gpu"]
            runtime = self.rocm if vendor == "amd" else self.nvidia
            expected = runtime.download_size(target)
            device = "AMD" if vendor == "amd" else "NVIDIA"

            def notify(message_id, transfer=None, downloaded=0, **values):
                if transfer:
                    values = {"progress": gpu_transfer_text(transfer, downloaded, expected, language)}
                self.gpu_note(message_id, language, device=device, **values)

            try:
                if vendor == "amd":
                    runtime.resolve(target)
                elif runtime.resolve(gpu) != (target, gpu):
                    raise GpuError(154)
                if runtime.installed(target, gpu):
                    notify(151)
                else:
                    runtime.launch_install(target, gpu, notify=notify)
            except GpuError as error:
                notify(error.message_id)
                raise

    def gpu_status(self, language):
        with self.gpu_lock:
            prompt = self.gpu_prompt
            rocm, nvidia = self.rocm.status(), self.nvidia.status()
        status = None
        for vendor, runtime in (("amd", rocm), ("nvidia", nvidia)):
            install = runtime["install"]
            if install["state"] not in {"installing", "checking"}:
                continue
            expected, downloaded = install.get("expected", 0), install.get("downloaded", 0)
            transfer = install.get("transfer")
            status = {
                "step": text(110 if vendor == "amd" else 136, language=language),
                "detail": (gpu_transfer_text(transfer, downloaded, expected, language) if transfer
                           else text(install.get("stage", 146), language=language)),
                "percent": min(99, 100 * downloaded / expected) if expected else 0,
            }
            if install["state"] == "checking":
                status["percent"] = 100
        public_prompt = {"id": prompt["id"], "message": prompt["message"]} if prompt else None
        return {"gpu_prompt": public_prompt, "gpu_progress": status,
                "rocm": rocm, "nvidia": nvidia}

    @property
    def paths(self):
        return self.workspace or normal_workspace()

    def folders(self, directory=""):
        paths = self.paths
        names = folder_paths(paths.images, directory, paths.excluded)
        self._ensure_count(directory)
        with self.lock:
            cached = self.folder_cache
        if cached is not None and cached[0] == directory:
            summaries = {item["path"]: item for item in cached[1]}
        else:
            summaries = {}
        return [
            summaries.get(name) or {
                "path": name,
                "name": text(84) if name == "" else Path(name).name,
                "total": 0,
                "tagged": 0,
                "sorted": 0,
                "scan": "none",
                "sort": "none",
            }
            for name in names
        ]

    def _ensure_count(self, directory=""):
        with self.lock:
            cached = self.folder_cache
            fresh = cached and cached[0] == directory and time.monotonic() - cached[2] < 8
            if fresh or self.folder_scan:
                return
            cancel = threading.Event()
            report_progress = self.workspace is not None and (not cached or cached[0] != directory)
            if report_progress:
                self.scan_progress = {"directory": directory, "stage": "discover", "done": 0, "total": None}
            worker = threading.Thread(target=self._count_folders, args=(directory, cancel, report_progress), daemon=True)
            self.folder_scan = (cancel, worker)
            worker.start()

    def stop_folder_count(self):
        with self.lock:
            if not self.folder_scan:
                return
            cancel, worker = self.folder_scan
            cancel.set()
        worker.join(timeout=2)

    def _invalidate_folders(self):
        self.folder_cache = None
        self.scan_progress = None
        if self.folder_scan:
            self.folder_scan[0].set()

    def remove_image(self, relative, revision):
        workspace = self.paths
        cleanup_pending = delete_source_image(workspace.images, relative, revision, index=workspace.csv,
                                              sorted_list=workspace.sorted_list, excluded=workspace.excluded)
        removed = Path(workspace.images).resolve() / relative
        with self.lock:
            if Path(self.preview_file) == removed:
                self.preview_file = ""
            if Path(self.current_file) == removed:
                self.current_file = ""
            self._invalidate_folders()
        return cleanup_pending

    def _count_folders(self, directory, cancel, report_progress):
        found = None
        last_stage = None
        last_report = 0.0

        def report(stage, done, total=None):
            nonlocal last_stage, last_report
            if stage == "done":
                return
            now = time.monotonic()
            if stage == last_stage and now - last_report < 0.2:
                return
            with self.lock:
                if not cancel.is_set():
                    self.scan_progress = {"directory": directory, "stage": stage, "done": done, "total": total}
                    last_stage, last_report = stage, now

        try:
            paths = self.paths
            found = source_folders(paths.images, directory=directory, csv=paths.csv, sorted_list=paths.sorted_list,
                                   exclude=paths.excluded, cancelled=cancel.is_set,
                                   progress=report if report_progress else None)
        except (OSError, ValueError) as error:
            self.note(text(27, error=error))
        finally:
            with self.lock:
                if found is not None and not cancel.is_set():
                    self.folder_cache = (directory, found, time.monotonic())
                    if report_progress:
                        total = sum(folder["total"] for folder in found)
                        self.scan_progress = {"directory": directory, "stage": "done", "done": total, "total": total}
                elif report_progress:
                    self.scan_progress = None
                self.folder_scan = None

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
                "scan_progress": self.scan_progress,
                "log": self.lines[-80:],
            }
        if preview:
            try:
                payload["preview_file"] = str(Path(preview).relative_to(self.paths.images))
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

    def start(self, mode, folders, *, language=None, device="cpu", architecture="auto", gpu="auto", destination=None):
        workspace = self.paths
        if destination is not None and destination not in ("vault", "output"):
            return text(171, language=language)
        if destination == "vault" and not workspace.excluded:
            return text(171, language=language)
        output = OUTPUT if destination == "output" else workspace.output
        paths, error = absolute_folders(folders, workspace.images, language=language, exclude=workspace.excluded)
        reason = error or preflight(mode, language=language, workspace=workspace, output=output)
        if reason:
            self.note(text(79, reason=reason, language=language))
            return reason
        tagging = None
        if mode in {"tag", "all"}:
            if device not in ("cpu", "amd", "nvidia"):
                return text(122, language=language)
            arch = uuid = python = None
            try:
                if device == "amd":
                    arch = self.rocm.resolve(architecture)
                    python = self.rocm.python_for(arch)
                elif device == "nvidia":
                    build, uuid = self.nvidia.resolve(gpu)
                    python = self.nvidia.python_for(build, uuid)
            except GpuError as error:
                return text(error.message_id, language=language)
            tagging = tag_command(paths, device=device, architecture=arch, gpu=uuid, python=python, workspace=workspace)
        with self.lock:
            if self.job_running:
                return text(33, language=language)
            self.job_running = True
            self.cancel_requested = False
            self.job_mode = mode
            self.percent = None
            self.detail = ""
            self.phase = "sort" if mode in {"sort", "plan"} else "tag"
            self.step = text(25, language=language)
            self._invalidate_folders()
        options = {"output": output} if self.workspace else {}
        threading.Thread(target=self._run, args=(mode, paths, language, tagging), kwargs=options, daemon=True).start()
        return None

    def _run(self, mode, folders, language, tagging, *, output=None):
        workspace = self.paths
        commands = [tagging] if tagging else []
        if mode in {"sort", "plan", "all"}:
            commands.append(sort_command(plan=mode == "plan", folders=folders, workspace=workspace, output=output))
        code = 0
        env = dict(os.environ, OUBI_JOB_LANGUAGE=language or read_lang(), PYTHONNOUSERSITE="1")
        try:
            for command in commands:
                with self.lock:
                    if self.cancel_requested:
                        code = -15
                        break
                    self.proc = subprocess.Popen(
                        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        env=env, cwd=workspace.data, text=True, bufsize=1, start_new_session=True,
                    )
                    proc = self.proc
                with proc.stdout:
                    for line in proc.stdout:
                        self.note(line)
                code = proc.wait()
                with self.lock:
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
                self._invalidate_folders()
            if code == 0:
                self.note(text(80, language=language))
            elif code < 0:
                self.note(text(81, language=language))
            else:
                self.note(text(82, code=code, language=language))

    def stop(self):
        with self.lock:
            self.cancel_requested = True
            proc = self.proc
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

    def _send(self, status, payload, content_type="application/json; charset=utf-8", *, cookie=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _language(self):
        return read_lang(header=self.headers.get("Accept-Language", ""))

    def _error(self, status, message):
        self._send(status, {"error": text(message, language=self._language())})

    def _vault_owner(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        entry = cookie.get(COOKIE_NAME)
        return bool(entry and self.app.vault.owns(entry.value))

    def _cookie(self, token):
        cookie = SimpleCookie()
        cookie[COOKIE_NAME] = token
        entry = cookie[COOKIE_NAME]
        entry["httponly"] = True
        entry["samesite"] = "Strict"
        entry["path"] = "/"
        if self.headers.get("X-Forwarded-Proto") == "https":
            entry["secure"] = True
        if not token:
            entry["max-age"] = 0
        return entry.OutputString()

    def _scoped_app(self, scope):
        if not scope:
            return self.app
        vault = self.app.vault
        if scope == vault.scope and self._vault_owner():
            return vault.app
        return None

    def _delete_image(self, payload):
        scope = payload.get("scope")
        path = payload.get("path")
        revision = payload.get("revision")
        if (self.headers.get_content_type() != "application/json"
                or not isinstance(scope, str) or not isinstance(path, str) or not isinstance(revision, str)
                or not revision or len(revision) > 160):
            self._error(400, 89)
            return
        with self.app.vault.lock:
            app = self._scoped_app(scope)
            if app is None:
                self._error(409, 172)
                return
            if app.status()["job_running"]:
                self._error(409, 188)
                return
            try:
                cleanup_pending = app.remove_image(path, revision)
            except ImageChangedError:
                self._error(409, 187)
                return
            except (OSError, UnicodeError, csv.Error):
                self._error(500, 186)
                return
            except ValueError:
                self._error(404, 87)
                return
            warning = text(189, language=self._language()) if cleanup_pending else ""
            self._send(200, {"ok": True, "warning": warning})

    def _vault_action(self, path, payload):
        if self.headers.get_content_type() != "application/json":
            self._error(400, 89)
            return
        vault = self.app.vault
        with vault.lock:
            try:
                if path == "/api/vault/unlock":
                    vault.unlock(payload.get("password"))
                    try:
                        workspace = Workspace.encrypted(vault.mount)
                        vault.app = App(workspace, devices=(self.app.rocm, self.app.nvidia))
                        vault.app._ensure_count()
                    except (OSError, ValueError, VaultError):
                        vault.close()
                        raise
                else:
                    if not self._vault_owner():
                        raise VaultError(172)
                    if vault.app.status()["job_running"]:
                        raise VaultError(173)
                    vault.close()
            except VaultError as error:
                self._error(409, error.message_id)
                return
            except (OSError, ValueError):
                self._error(409, 169)
                return
            self._send(200, {"scope": vault.scope}, cookie=self._cookie(vault.token))

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        path = parsed.path
        vault = self.app.vault
        scope = query.get("scope", [""])[0]
        if path == "/api/status":
            gpu = self.app.gpu_status(self._language())
            normal = self.app.status()
            scope, opened_app = vault.scope, vault.app
            state = normal
            authorized = bool(opened_app and self._vault_owner())
            if authorized:
                vault_state = opened_app.status()
                # Scoped filesystem requests can hold the vault lock while the NAS responds.
                authorized = opened_app is vault.app and scope == vault.scope
                if authorized:
                    state = vault_state
                    if state["phase"] == "download":
                        for field in ("step", "detail", "percent"):
                            state[field] = normal[field] if normal["phase"] == "download" else None
            self._send(200, {**state, **gpu, "model": normal["model"],
                             "vault_enabled": vault.path is not None,
                             "vault_scope": scope if authorized else ""})
            return
        if path == "/":
            with vault.lock:
                opened = self._vault_owner()
                workspace = vault.app if opened else self.app
                page = page_bytes(
                    workspace.folders(), read_theme(), read_genres(), read_layout(),
                    self._language(), load_catalogs(), read_language_choice(),
                    vault_enabled=vault.path is not None, vault_scope=vault.scope if opened else "",
                )
            self._send(200, page, "text/html; charset=utf-8")
            return
        with vault.lock if scope else nullcontext():
            app = self._scoped_app(scope)
            if app is None:
                self._error(409, 172)
                return
            self._get_files(path, query, app)

    def _get_files(self, path, query, app):
        workspace = app.paths
        rel = query.get("path", [""])[0]
        directories = query.get("folder", [])
        from_output = query.get("root", ["output" if path == "/api/browse" else "images"])[0] == "output"
        root = workspace.output if from_output else workspace.images
        excluded = None if from_output else workspace.excluded

        try:
            if path == "/api/folders":
                self._send(200, app.folders(directories[0] if directories else ""))
                return
            if path == "/api/recent":
                files = recent_tagged(directories, root=workspace.images, exclude=workspace.excluded)
                self._send(200, {"files": files})
                return
            if path == "/api/tagged":
                files = images_with_tags(query.get("tag", []), path=workspace.csv,
                                         root=workspace.images, exclude=workspace.excluded)
                self._send(200, {"files": files})
                return
            if path == "/api/vocabulary":
                tags = tags_in_folders(directories, path=workspace.csv, root=workspace.images,
                                       exclude=workspace.excluded)
                self._send(200, {"tags": tags})
                return
            if path == "/api/browse":
                if in_output(resolve_under(root, rel), excluded):
                    raise ValueError(rel)
                data = browse_dir(root, rel)
                if excluded:
                    data["items"] = [item for item in data["items"] if not in_output(Path(root) / item["path"], excluded)]
                self._send(200, data)
                return
            if path == "/api/tags":
                file = resolve_under(workspace.images, rel)
                if in_output(file, workspace.excluded):
                    raise ValueError(rel)
                data = read_info(rel, root=workspace.images, csv=workspace.csv, encrypted=bool(workspace.excluded))
                if file.suffix.lower() in IMAGE_EXTS and file.is_file():
                    data["revision"] = image_revision(file)
                self._send(200, data)
                return
            if path == "/api/file":
                file = resolve_under(root, rel)
                if not file.is_file() or file.suffix.lower() not in IMAGE_EXTS or in_output(file, excluded):
                    raise ValueError(rel)
                self._send(200, file.read_bytes(), content_type(file))
                return
        except (OSError, ValueError):
            self._error(404, 87 if path in {"/api/tags", "/api/file"} else 86)
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
        if parsed.path in {"/api/vault/unlock", "/api/vault/lock"}:
            self._vault_action(parsed.path, payload)
            return
        if parsed.path == "/api/image/delete":
            self._delete_image(payload)
            return
        if parsed.path == "/api/stop":
            scope = parse_qs(parsed.query).get("scope", [""])[0]
            with self.app.vault.lock if scope else nullcontext():
                app = self._scoped_app(scope)
                if app is None:
                    self._error(409, 172)
                    return
                app.stop()
                self._send(200, {"ok": True})
            return
        if parsed.path == "/api/download":
            if not self.app.retry_download():
                self._error(409, 99)
                return
            self._send(200, {"ok": True})
            return
        if parsed.path in {"/api/rocm", "/api/nvidia", "/api/terminal"}:
            if self.headers.get_content_type() != "application/json":
                self._error(400, 89)
                return
            language = self._language()
            try:
                if parsed.path == "/api/terminal":
                    self.app.answer_gpu_install(payload.get("id"), payload.get("answer"), language)
                else:
                    vendor, field = ("amd", "architecture") if parsed.path == "/api/rocm" else ("nvidia", "gpu")
                    self.app.request_gpu_install(vendor, payload.get(field, "auto"), language)
            except GpuError as error:
                if parsed.path != "/api/terminal":
                    self.app.gpu_note(error.message_id, language)
                self._error(409, error.message_id)
                return
            self._send(202, {"ok": True})
            return
        if parsed.path != "/api/job":
            self._error(404, 88)
            return
        scope = payload.get("scope", "")
        if not isinstance(scope, str):
            self._error(400, 89)
            return
        with self.app.vault.lock:
            app = self._scoped_app(scope)
            if app is None:
                self._error(409, 172)
                return
            mode = payload.get("mode")
            normal = self.app.status()
            vault_app = self.app.vault.app
            busy = normal["job_running"] or bool(vault_app and vault_app.status()["job_running"])
            language = self._language()
            reason = reject_reason(normal["model"], busy, mode, language=language)
            if reason is None:
                reason = app.start(
                    mode, payload.get("folders") or [], language=language,
                    device=payload.get("device", "cpu"), architecture=payload.get("architecture", "auto"),
                    gpu=payload.get("gpu", "auto"), destination=payload.get("destination"),
                )
            if reason:
                self._send(409, {"error": reason})
                return
            self._send(200, {"ok": True})


def main():
    app = App()
    Handler.app = app
    port = int(os.environ.get("OUBI_LISTEN", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=app.download, daemon=True).start()
    app._ensure_count()
    print(text(28, port=port), flush=True)
    names = folder_names()
    if not names:
        print(text(29), flush=True)
    elif names == [""]:
        print(text(30), flush=True)
    else:
        print(text(31, count=len(names)), flush=True)
    def shutdown(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        with app.vault.lock:
            if opened := app.vault.app:
                opened.stop()
                deadline = time.monotonic() + 5
                while opened.status()["job_running"] and time.monotonic() < deadline:
                    time.sleep(0.05)
            try:
                app.vault.close()
            except VaultError as error:
                print(text(error.message_id), flush=True)


if __name__ == "__main__":
    main()
