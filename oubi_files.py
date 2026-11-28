"""Paths shared by the tagger, sorter, and web viewer."""

import csv
import os
import stat
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

JPEG_XL_SUFFIX = ".jxl"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", JPEG_XL_SUFFIX}
VAULT_MOUNT = Path("/mnt/oubi-crypt/mount")
VAULT_METADATA_ROOT = Path("/run/oubi-crypt/mount")
WORKSPACE_METADATA_DIR = ".oubi"


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


def is_workspace_metadata(path):
    return WORKSPACE_METADATA_DIR in Path(path).parts


def open_image(source, *, jpeg_xl=False):
    from PIL import Image

    if jpeg_xl:
        try:
            import pillow_jxl  # Registers the JPEG XL Pillow plugin.
        except ImportError as error:
            raise OSError("JPEG XL decoder is unavailable") from error
    return Image.open(source)


def direct_image_files(folder):
    with os.scandir(folder) as entries:
        for entry in entries:
            if Path(entry.name).suffix.lower() in IMAGE_EXTS and entry.is_file(follow_symlinks=False):
                yield Path(entry.path)


def image_files(folder, exclude=None, *, cancelled=None, visit=None, onerror=None):
    if is_workspace_metadata(folder):
        return
    excluded = Path(exclude).resolve() if exclude else None
    for directory, subdirs, names in os.walk(folder, onerror=onerror):
        if cancelled and cancelled():
            return
        parent = Path(directory)
        if excluded and parent.resolve().is_relative_to(excluded):
            subdirs.clear()
            continue
        subdirs[:] = [name for name in subdirs if name not in {"tags", WORKSPACE_METADATA_DIR}
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
    return file_revision(path.lstat())


def file_revision(info):
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Image must be a regular file")
    fields = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return "-".join(str(field) for field in fields)


def _updated_index(index, root, images, *, table, cleanup, moved=None):
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
        return bool(raw) and Path(os.path.normpath(indexed_image_path(raw, root))) in images

    def relocated(raw):
        path = moved[Path(os.path.normpath(indexed_image_path(raw, root)))]
        return str(stored_image_path(path) if Path(raw).is_absolute() else path.relative_to(root))

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
                    if moved is not None:
                        row[field] = relocated(row[field])
                        writer.writerow(row)
                else:
                    writer.writerow(row)
        else:
            for line in source:
                raw = line.rstrip("\r\n")
                if matches(raw):
                    removed = True
                    if moved is not None:
                        output.write(relocated(raw) + line[len(raw):])
                else:
                    output.write(line)
    if not removed:
        return None
    updated.chmod(stat.S_IMODE(index.stat().st_mode))
    return updated


def stage_migrated_indexes(root, moved, index, sorted_list, cleanup):
    changes = []
    for path, table in ((Path(index), True), (Path(sorted_list), False)):
        updated = _updated_index(path, root, moved, table=table, cleanup=cleanup, moved=moved)
        if updated is not None:
            changes.append((path, updated))
    return changes


def apply_index_changes(changes):
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
    return delete_source_images(root, [(relative, revision)], index=index, sorted_list=sorted_list,
                                excluded=excluded, before_commit=before_commit)


def source_image_path(root, relative, excluded=None):
    path = Path(relative)
    image = root / path
    if (path.is_absolute() or not path.parts or any(part in {"..", "tags", WORKSPACE_METADATA_DIR} for part in path.parts)
            or path.suffix.lower() not in IMAGE_EXTS or image.parent.resolve() != image.parent
            or (excluded and image.is_relative_to(excluded))):
        raise ValueError("Invalid source image")
    return image


def delete_source_images(root, files, *, index, sorted_list, excluded=None, before_commit=None):
    root = Path(root).resolve()
    excluded = Path(excluded).resolve() if excluded else None
    images = {}
    changes = []

    def check_revision(image, relative, revision):
        if image.parent.resolve() != image.parent:
            raise ImageChangedError(relative)
        try:
            current = image_revision(image)
        except FileNotFoundError as error:
            raise ImageChangedError(relative) from error
        if current != revision:
            raise ImageChangedError(relative)

    for relative, revision in files:
        image = source_image_path(root, relative, excluded)
        if image in images:
            raise ValueError("Invalid source image")
        check_revision(image, relative, revision)
        images[image] = (relative, revision)
        changes.append((image, None))
        tags = sidecar_path(image)
        if tags.parent.is_symlink() or tags.is_symlink() or (tags.exists() and not tags.is_file()):
            raise ValueError("Invalid image sidecar")
        if tags.is_file():
            changes.append((tags, None))

    with ExitStack() as cleanup:
        for path, table in ((Path(index), True), (Path(sorted_list), False)):
            updated = _updated_index(path, root, images, table=table, cleanup=cleanup)
            if updated is not None:
                changes.append((path, updated))
        for image, (relative, revision) in images.items():
            check_revision(image, relative, revision)
        if before_commit is not None:
            before_commit()
        return apply_index_changes(changes)
