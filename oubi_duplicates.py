"""Perceptual image matching and the per-workspace review queue."""
import json
import os
import sqlite3
import stat
from collections import defaultdict
from contextlib import closing
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock

from oubi_files import IMAGE_EXTS, JPEG_XL_SUFFIX, file_revision, image_files, image_revision, open_image

DATABASE_NAME = "duplicates.sqlite3"
MAX_DISTANCE = 8
HASH_BITS = 63
PAIR_COLUMNS = ("path", "revision", "width", "height", "size")


class DuplicateError(Exception):
    def __init__(self, code, **values):
        self.code = code
        self.values = values
        super().__init__(code)


@lru_cache(maxsize=1)
def dct_basis():
    import numpy as np

    positions = np.arange(32) + 0.5
    return np.cos(np.pi / 32 * np.arange(8)[:, None] * positions)


def fingerprint(path):
    import numpy as np
    from PIL import Image, ImageOps

    with open_image(path, jpeg_xl=path.suffix.lower() == JPEG_XL_SUFFIX) as source:
        image = ImageOps.exif_transpose(source)
        width, height = image.size
        sample = image.convert("RGBA").resize((32, 32), Image.Resampling.LANCZOS)
    background = Image.new("RGBA", sample.size, "white")
    pixels = np.asarray(Image.alpha_composite(background, sample).convert("L"), dtype=np.float64)
    basis = dct_basis()
    coefficients = np.round((basis @ (pixels - pixels.mean()) @ basis.T).ravel()[1:], 6)
    median = np.median(coefficients)
    phash = sum(1 << i for i, value in enumerate(coefficients) if value > median)
    return phash, width, height


def candidate_path(workspace, relative):
    root = Path(workspace.images).resolve()
    rel = Path(relative)
    if (rel.is_absolute() or rel.as_posix() != relative or not rel.parts
            or any(part == ".." or part == "tags" or part.startswith(".") for part in rel.parts)
            or rel.suffix.lower() not in IMAGE_EXTS):
        raise ValueError(relative)
    path = root / rel
    if path.parent.resolve() != path.parent or (workspace.excluded and path.is_relative_to(workspace.excluded)):
        raise ValueError(relative)
    return path


def duplicate_file(workspace, relative):
    path = candidate_path(workspace, relative)
    info = path.lstat()
    revision = file_revision(info)
    # A Cryptomator remount can assign new device and inode numbers.
    saved_revision = f"{info.st_size}:{info.st_mtime_ns}" if workspace.data else revision
    return path, saved_revision, revision, info.st_size


def matching_pairs(entries, cancelled):
    # At distance <= 8, at least one of the nine 7-bit bands must match.
    bands = HASH_BITS // (MAX_DISTANCE + 1)
    mask = (1 << bands) - 1
    same_hash = defaultdict(list)
    for entry in entries:
        same_hash[entry[2]].append(entry[0])
    buckets = defaultdict(list)
    for phash, paths in same_hash.items():
        if cancelled():
            return
        for left, right in combinations(paths, 2):
            if cancelled():
                return
            yield (*sorted((left, right)), 0)
        keys = [(band, (phash >> (band * bands)) & mask) for band in range(MAX_DISTANCE + 1)]
        for other in {hash_ for key in keys for hash_ in buckets[key]}:
            distance = (phash ^ other).bit_count()
            if distance <= MAX_DISTANCE:
                for left in paths:
                    for right in same_hash[other]:
                        if cancelled():
                            return
                        yield (*sorted((left, right)), distance)
        for key in keys:
            buckets[key].append(phash)


class DuplicateStore:
    def __init__(self, workspace):
        self.path = Path(workspace.data or Path(workspace.csv).parent) / DATABASE_NAME
        self.lock = RLock()
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute("PRAGMA trusted_schema = OFF")
        try:
            try:
                mode = self.path.lstat().st_mode
            except FileNotFoundError:
                self.db.executescript("""
                    CREATE TABLE images (
                        path TEXT PRIMARY KEY, revision TEXT NOT NULL, phash INTEGER NOT NULL,
                        width INTEGER NOT NULL, height INTEGER NOT NULL, size INTEGER NOT NULL
                    );
                    CREATE TABLE pairs (
                        left_path TEXT NOT NULL, right_path TEXT NOT NULL, distance INTEGER NOT NULL,
                        PRIMARY KEY (left_path, right_path)
                    ) WITHOUT ROWID;
                    CREATE INDEX pair_order ON pairs(distance, left_path, right_path);
                    CREATE TABLE search (completed INTEGER NOT NULL);
                    INSERT INTO search VALUES (0);
                    CREATE TABLE dismissed (
                        left_path TEXT NOT NULL, right_path TEXT NOT NULL,
                        left_revision TEXT NOT NULL, right_revision TEXT NOT NULL,
                        PRIMARY KEY (left_path, right_path)
                    ) WITHOUT ROWID;
                """)
            else:
                if not stat.S_ISREG(mode):
                    raise OSError("Duplicate database must be a regular file")
                self.db.deserialize(self.path.read_bytes())
                if self.db.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                    raise sqlite3.DatabaseError("Duplicate database failed its integrity check")
                for table in ("images", "pairs", "dismissed", "search"):
                    self.db.execute(f"SELECT * FROM {table} LIMIT 0")
        except (OSError, sqlite3.Error) as error:
            self.db.close()
            raise DuplicateError(276, error=str(error)) from error

    def copy(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA trusted_schema = OFF")
        with self.lock:
            self.db.backup(connection)
        return connection

    def install(self, connection, *, retain_on_error=False):
        connection.commit()
        snapshot = connection.serialize()
        try:
            with NamedTemporaryFile(dir=self.path.parent, prefix=".duplicates-", delete=False) as handle:
                temporary = Path(handle.name)
                try:
                    handle.write(snapshot)
                    handle.flush()
                    os.fsync(handle.fileno())
                    handle.close()
                    temporary.replace(self.path)
                finally:
                    temporary.unlink(missing_ok=True)
        except OSError:
            if retain_on_error:
                with self.lock:
                    self.db.deserialize(snapshot)
            raise
        with self.lock:
            self.db.deserialize(snapshot)

    def page(self, index, pending_files=(), pending_pairs=()):
        where = ""
        params = []
        if pending_files or pending_pairs:
            where = """WHERE p.left_path NOT IN (SELECT value FROM json_each(?1))
                         AND p.right_path NOT IN (SELECT value FROM json_each(?1))
                         AND (p.left_path, p.right_path) NOT IN (
                             SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') FROM json_each(?2)
                         )"""
            params = [json.dumps(list(pending_files)), json.dumps(pending_pairs)]
        count, = self.db.execute(f"SELECT COUNT(*) FROM pairs AS p {where}", params).fetchone()
        completed, = self.db.execute("SELECT completed FROM search").fetchone()
        index = min(index, max(0, count - 1))
        rows = self.db.execute(f"""
            SELECT a.path, a.revision, a.width, a.height, a.size,
                   b.path, b.revision, b.width, b.height, b.size, p.distance
            FROM pairs AS p JOIN images AS a ON a.path = p.left_path
            JOIN images AS b ON b.path = p.right_path
            {where} ORDER BY p.distance, p.left_path, p.right_path LIMIT 2 OFFSET ?
        """, (*params, index)).fetchall()
        pairs = [{"images": [dict(zip(PAIR_COLUMNS, row[i:i+5])) for i in (0, 5)],
                  "distance": row[-1]} for row in rows]
        return {"searched": bool(completed), "count": count, "index": index, "pair": pairs[0] if pairs else None,
                "next": pairs[1] if len(pairs) > 1 else None}

    def contains(self, left, right, left_revision, right_revision):
        return self.db.execute("""
            SELECT 1 FROM pairs AS p JOIN images AS a ON a.path = p.left_path
            JOIN images AS b ON b.path = p.right_path
            WHERE p.left_path = ? AND p.right_path = ? AND a.revision = ? AND b.revision = ?
        """, (left, right, left_revision, right_revision)).fetchone() is not None

    def dismiss(self, pair):
        with closing(self.copy()) as connection:
            connection.execute("INSERT OR REPLACE INTO dismissed VALUES (?, ?, ?, ?)", pair)
            connection.execute("DELETE FROM pairs WHERE left_path = ? AND right_path = ?", pair[:2])
            self.install(connection)

    def drop_pairs(self, *relatives):
        self.db.executemany("DELETE FROM pairs WHERE left_path = ? OR right_path = ?",
                            ((relative, relative) for relative in relatives))
        self.db.commit()

    def remove(self, *relatives):
        with closing(self.copy()) as connection:
            for table in ("pairs", "dismissed"):
                connection.executemany(f"DELETE FROM {table} WHERE left_path = ? OR right_path = ?",
                                       ((relative, relative) for relative in relatives))
            connection.executemany("DELETE FROM images WHERE path = ?", ((relative,) for relative in relatives))
            self.install(connection, retain_on_error=True)


def scan_duplicates(store, workspace, folders, cancelled, progress, report_error):
    root = Path(workspace.images).resolve()
    candidates = set()
    for folder in folders:
        try:
            parent = Path(folder).relative_to(root)
            if any(part == "tags" or part.startswith(".") for part in parent.parts):
                raise ValueError(folder)
            for path in image_files(folder, workspace.excluded, cancelled=cancelled,
                                    visit=lambda _: None, onerror=raise_walk_error):
                candidates.add(path.relative_to(root).as_posix())
                progress(272, len(candidates), None)
        except (OSError, ValueError) as error:
            raise DuplicateError(277, error=str(error)) from error
    if cancelled():
        return None

    from PIL import Image

    with closing(store.copy()) as connection:
        cached = {row[0]: row for row in connection.execute("SELECT * FROM images")}
        entries = []
        for done, relative in enumerate(sorted(candidates), 1):
            if cancelled():
                return None
            try:
                path, saved_revision, revision, size = duplicate_file(workspace, relative)
            except (OSError, ValueError) as error:
                report_error(278, path=relative, error=str(error))
                continue
            entry = cached.get(relative)
            if entry is None or entry[1] != saved_revision:
                try:
                    phash, width, height = fingerprint(path)
                except (OSError, ValueError, RuntimeError, Image.DecompressionBombError) as error:
                    report_error(279, path=relative, error=str(error))
                    continue
                try:
                    if image_revision(path) != revision:
                        raise ValueError("File changed while reading")
                except (OSError, ValueError) as error:
                    report_error(293, path=relative, error=str(error))
                    continue
                entry = (relative, saved_revision, phash, width, height, size)
            entries.append(entry)
            progress(273, done, len(candidates))

        progress(274, 0, None)
        connection.executemany("INSERT OR REPLACE INTO images VALUES (?, ?, ?, ?, ?, ?)", entries)
        connection.execute("DELETE FROM pairs")
        connection.executemany("INSERT INTO pairs VALUES (?, ?, ?)", matching_pairs(entries, cancelled))
        connection.execute("""
            DELETE FROM pairs AS p WHERE EXISTS (
                SELECT 1 FROM dismissed AS d
                JOIN images AS a ON a.path = d.left_path AND a.revision = d.left_revision
                JOIN images AS b ON b.path = d.right_path AND b.revision = d.right_revision
                WHERE p.left_path = d.left_path AND p.right_path = d.right_path
            )
        """)
        if cancelled():
            return None
        count, = connection.execute("SELECT COUNT(*) FROM pairs").fetchone()
        connection.execute("UPDATE search SET completed = 1")
        try:
            store.install(connection)
        except OSError as error:
            raise DuplicateError(280, error=str(error)) from error
        return count


def raise_walk_error(error):
    raise error
