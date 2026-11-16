"""Rebuildable SQLite catalog for one unlocked Cryptomator workspace."""
import csv
import errno
import json
import os
import sqlite3
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile

from oubi_files import sidecar_path

CHANGE_EVENT = "OUBI_INDEX_CHANGES "
INDEX_NAME = "index.sqlite3"
DIRTY_NAME = "index.dirty"
VERSION = 1
TAG_COLUMNS = ("rating", "character", "copyright", "style", "general", "meta")


def report_changes(paths):
    if os.environ.get("OUBI_INDEX_EVENTS") == "1":
        print(CHANGE_EVENT + json.dumps([str(path) for path in paths]), flush=True)


def relative_path(value, root):
    if not value:
        return None
    try:
        return Path(os.path.normpath(root / value)).relative_to(root).as_posix()
    except ValueError:
        return None


def tags_from(text):
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def read_metadata(workspace):
    root = Path(workspace.images)
    rows = {}
    file = Path(workspace.csv)
    if file.is_file():
        csv.field_size_limit(10**9)
        with file.open(encoding="utf-8", errors="replace", newline="") as handle:
            for row in csv.DictReader(handle):
                path = relative_path(row.get("file"), root)
                if path is not None:
                    rows[path] = row
    file = Path(workspace.sorted_list)
    sorted_paths = set()
    if file.is_file():
        with file.open(encoding="utf-8", errors="replace") as handle:
            sorted_paths.update(relative_path(line.rstrip("\r\n"), root) for line in handle)
    return rows, sorted_paths


def folder_sidecars(folder):
    directory = folder / "tags"
    if directory.is_symlink():
        return {}
    try:
        with os.scandir(directory) as entries:
            return {entry.name: entry for entry in entries
                    if entry.name.endswith(".txt") and entry.is_file(follow_symlinks=False)}
    except (FileNotFoundError, NotADirectoryError):
        return {}


def image_metadata(image, row, *, sidecars=None):
    sidecar = sidecar_path(image)
    try:
        if sidecars is None:
            info = sidecar.lstat()
        else:
            entry = sidecars.get(sidecar.name)
            info = entry.stat(follow_symlinks=False) if entry is not None else None
    except FileNotFoundError:
        info = None
    sidecar_text = None
    if info is not None and stat.S_ISREG(info.st_mode):
        try:
            sidecar_text = sidecar.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            pass
    if sidecar_text is not None:
        modified = info.st_mtime
    else:
        modified = image.stat().st_mtime if row is not None else 0
    row_tags = [tag for column in TAG_COLUMNS for tag in tags_from((row or {}).get(column))]
    character = tags_from((row or {}).get("character"))[:1]
    details = sidecar_text if sidecar_text is not None else ", ".join(row_tags)
    terms = {tag.casefold(): tag for tag in row_tags + tags_from(sidecar_text)}
    return (row is not None or sidecar_text is not None, modified, "".join(character), details[:4000], terms)


def folder_clause(directories, column):
    if "" in directories:
        return "1", []
    if len(directories) > 100:
        return (f"EXISTS (SELECT 1 FROM json_each(?) WHERE {column} >= value || '/' AND {column} < value || '0')",
                [json.dumps(directories)])
    conditions, values = [], []
    for name in directories:
        conditions.append(f"({column} >= ? AND {column} < ?)")
        values.extend((name + "/", name + "0"))
    return " OR ".join(conditions) or "0", values


class VaultIndex:
    def __init__(self, workspace):
        self.directory = Path(workspace.data)
        self.excluded = relative_path(workspace.excluded, Path(workspace.images))
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.term_ids = {}
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA trusted_schema = OFF")
        self.db.execute("PRAGMA temp_store = MEMORY")

    def initialize(self):
        self.db.executescript(f"""
            PRAGMA user_version = {VERSION};
            CREATE TABLE config (excluded TEXT);
            CREATE TABLE folders (path TEXT PRIMARY KEY, parent TEXT);
            CREATE INDEX folder_parent ON folders(parent);
            CREATE TABLE images (
                id INTEGER PRIMARY KEY, path TEXT UNIQUE, tagged INTEGER, sorted INTEGER,
                modified REAL, character TEXT, details TEXT
            );
            CREATE TABLE terms (id INTEGER PRIMARY KEY, key TEXT UNIQUE, label TEXT);
            CREATE TABLE tags (
                image INTEGER REFERENCES images(id) ON DELETE CASCADE,
                term INTEGER REFERENCES terms(id), PRIMARY KEY(image, term)
            ) WITHOUT ROWID;
            CREATE INDEX tag_search ON tags(term, image);
        """)
        self.db.execute("INSERT INTO config VALUES (?)", (self.excluded,))
        return self

    def restore(self):
        snapshot = self.directory / INDEX_NAME
        if (self.directory / DIRTY_NAME).exists() or not snapshot.is_file() or snapshot.is_symlink():
            return False
        self.db.deserialize(snapshot.read_bytes())
        return (self.db.execute("PRAGMA user_version").fetchone()[0] == VERSION
                and self.db.execute("SELECT excluded FROM config").fetchone() == (self.excluded,)
                and self.db.execute("PRAGMA quick_check(1)").fetchone() == ("ok",))

    def _sync_directory(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
        finally:
            os.close(fd)

    def mark_dirty(self):
        marker = self.directory / DIRTY_NAME
        try:
            with marker.open("xb") as handle:
                os.fsync(handle.fileno())
            self._sync_directory()
        except FileExistsError:
            if marker.is_symlink() or not marker.is_file():
                raise

    def save(self):
        self.db.commit()
        snapshot = self.db.serialize()
        with NamedTemporaryFile(dir=self.directory, prefix=".index-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(snapshot)
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                temporary.replace(self.directory / INDEX_NAME)
                self._sync_directory()
                (self.directory / DIRTY_NAME).unlink(missing_ok=True)
            finally:
                temporary.unlink(missing_ok=True)

    def add_folder(self, name):
        parent = name.rpartition("/")[0] if name else None
        self.db.execute("INSERT OR IGNORE INTO folders VALUES (?, ?)", (name, parent))

    def set_image(self, name, tagged, sorted_, modified, character, details, terms):
        self.db.execute("DELETE FROM images WHERE path = ?", (name,))
        image = self.db.execute("INSERT INTO images(path, tagged, sorted, modified, character, details) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (name, tagged, sorted_, modified, character, details)).lastrowid
        for key, label in terms.items():
            if key not in self.term_ids:
                self.db.execute("INSERT OR IGNORE INTO terms(key, label) VALUES (?, ?)", (key, label))
                self.term_ids[key] = self.db.execute("SELECT id FROM terms WHERE key = ?", (key,)).fetchone()[0]
        self.db.executemany("INSERT INTO tags VALUES (?, ?)", ((image, self.term_ids[key]) for key in terms))

    def remove(self, name):
        self.db.execute("DELETE FROM images WHERE path = ?", (name,))

    def paths(self):
        return (row[0] for row in self.db.execute("SELECT path FROM images"))

    def children(self, name):
        if self.db.execute("SELECT 1 FROM folders WHERE path = ?", (name,)).fetchone() is None:
            raise ValueError(name)
        children = [row[0] for row in self.db.execute("SELECT path FROM folders WHERE parent = ?", (name,))]
        return sorted(children, key=str.casefold) or [name]

    def totals(self, name):
        condition, params = folder_clause([name], "path")
        row = self.db.execute(f"SELECT COUNT(*), SUM(tagged), SUM(sorted) FROM images WHERE {condition}", params).fetchone()
        return tuple(value or 0 for value in row)

    def gallery(self, folders):
        condition, params = folder_clause(folders, "images.path")
        return [row[0] for row in self.db.execute(
            f"SELECT path FROM images WHERE tagged AND ({condition}) ORDER BY modified, path", params)]

    def search(self, folders, tags):
        keys = sorted({tag.strip().casefold() for tag in tags if tag.strip()})
        if not keys:
            return []
        condition, params = folder_clause(folders, "images.path")
        placeholders = ", ".join("?" for _ in keys)
        return [row[0] for row in self.db.execute(
            "SELECT images.path FROM terms JOIN tags ON tags.term = terms.id JOIN images ON images.id = tags.image "
            f"WHERE terms.key IN ({placeholders}) AND ({condition}) "
            "GROUP BY images.id HAVING COUNT(*) = ? ORDER BY images.path", [*keys, *params, len(keys)])]

    def vocabulary(self, folders):
        condition, params = folder_clause(folders, "images.path")
        return [row[0] for row in self.db.execute(
            "SELECT terms.label FROM terms JOIN tags ON tags.term = terms.id JOIN images ON images.id = tags.image "
            f"WHERE {condition} GROUP BY terms.id ORDER BY terms.key", params)]

    def details(self, name):
        row = self.db.execute("SELECT character, details FROM images WHERE path = ?", (name,)).fetchone()
        if row is not None:
            return {"character": row[0], "text": row[1]}
        return None
