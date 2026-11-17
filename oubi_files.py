"""Paths shared by the tagger, sorter, and web viewer."""

import csv
import os
import stat
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VAULT_MOUNT = Path("/mnt/oubi-crypt/mount")
VAULT_METADATA_ROOT = Path("/run/oubi-crypt/mount")


def indexed_image_path(value, root):
    path = Path(value)
    if root == VAULT_MOUNT and path.is_relative_to(VAULT_METADATA_ROOT):
        path = path.relative_to(VAULT_METADATA_ROOT)
    return root / path


def stored_image_path(path):
    # Keep metadata readable by containers that still mount the vault at the old path.
    if path.is_relative_to(VAULT_MOUNT):
        return VAULT_METADATA_ROOT / path.relative_to(VAULT_MOUNT)
    return path


def sidecar_path(image):
    image = Path(image)
    return image.parent / "tags" / (image.name + ".txt")


def direct_image_files(folder):
    with os.scandir(folder) as entries:
        for entry in entries:
            if Path(entry.name).suffix.lower() in IMAGE_EXTS and entry.is_file(follow_symlinks=False):
                yield Path(entry.path)


def image_files(folder, exclude=None, *, cancelled=None, visit=None, onerror=None):
    excluded = Path(exclude).resolve() if exclude else None
    for directory, subdirs, names in os.walk(folder, onerror=onerror):
        if cancelled and cancelled():
            return
        parent = Path(directory)
        if excluded and parent.resolve().is_relative_to(excluded):
            subdirs.clear()
            continue
        subdirs[:] = [name for name in subdirs if name != "tags"
                      and not (excluded and (parent / name).resolve() == excluded)]
        if visit is not None:
            subdirs[:] = [name for name in subdirs if not name.startswith(".") and not (parent / name).is_symlink()]
            visit(parent)
        for name in names:
            if cancelled and cancelled():
                return
            image = parent / name
            if image.suffix.lower() in IMAGE_EXTS and image.is_file() and not (excluded and image.is_symlink()):
                yield image


class ImageChangedError(Exception):
    """The file no longer matches the preview selected for removal."""


def image_revision(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Image must be a regular file")
    fields = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return "-".join(str(field) for field in fields)


def _updated_index(index, root, image, *, table, cleanup):
    if index.is_symlink():
        raise ValueError("Image index must be a regular file")
    if not index.exists():
        return None
    if not index.is_file():
        raise ValueError("Image index must be a regular file")
    staging = Path(cleanup.enter_context(TemporaryDirectory(prefix=".oubi-index-", dir=index.parent)))
    updated = staging / "updated"
    removed = False

    def matches(raw):
        return bool(raw) and Path(os.path.normpath(indexed_image_path(raw, root))) == image

    with index.open(encoding="utf-8", newline="") as source, updated.open("w", encoding="utf-8", newline="") as output:
        if table:
            csv.field_size_limit(10**9)
            reader = csv.reader(source, strict=True)
            header = next(reader, [])
            if "file" not in header:
                return None
            field = header.index("file")
            writer = csv.writer(output)
            writer.writerow(header)
            for row in reader:
                if len(row) > field and matches(row[field]):
                    removed = True
                else:
                    writer.writerow(row)
        else:
            for line in source:
                if matches(line.rstrip("\r\n")):
                    removed = True
                else:
                    output.write(line)
    if not removed:
        return None
    updated.chmod(stat.S_IMODE(index.stat().st_mode))
    return updated


def _apply_removal(changes):
    backups = []
    try:
        for path, replacement in changes:
            backup = path.with_name(".oubi-delete-" + uuid4().hex)
            path.rename(backup)
            backups.append((path, backup))
            if replacement is not None:
                replacement.replace(path)
    except OSError:
        for path, backup in reversed(backups):
            try:
                backup.replace(path)
            except OSError:
                # Keep the backup if the filesystem also refused to restore the original.
                continue
        raise

    cleanup_pending = False
    for _, backup in backups:
        try:
            backup.unlink()
        except OSError:
            cleanup_pending = True
    return cleanup_pending


def delete_source_image(root, relative, revision, *, index, sorted_list, excluded=None, before_commit=None):
    root = Path(root).resolve()
    path = Path(relative)
    if (path.is_absolute() or not path.parts or any(part in {"..", "tags", ".oubi"} for part in path.parts)
            or path.suffix.lower() not in IMAGE_EXTS):
        raise ValueError("Invalid source image")
    image = root / path
    if image.parent.resolve() != image.parent or (excluded and image.is_relative_to(Path(excluded).resolve())):
        raise ValueError("Invalid source image")

    def check_revision():
        if image.parent.resolve() != image.parent:
            raise ImageChangedError()
        try:
            current = image_revision(image)
        except FileNotFoundError as error:
            raise ImageChangedError() from error
        if current != revision:
            raise ImageChangedError()

    check_revision()
    tags = sidecar_path(image)
    if tags.parent.is_symlink() or tags.is_symlink() or (tags.exists() and not tags.is_file()):
        raise ValueError("Invalid image sidecar")

    with ExitStack() as cleanup:
        changes = [(image, None)]
        if tags.is_file():
            changes.append((tags, None))
        for path, table in ((Path(index), True), (Path(sorted_list), False)):
            updated = _updated_index(path, root, image, table=table, cleanup=cleanup)
            if updated is not None:
                changes.append((path, updated))
        check_revision()
        if before_commit is not None:
            before_commit()
        return _apply_removal(changes)
