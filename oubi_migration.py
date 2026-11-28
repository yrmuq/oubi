"""Move images and their metadata into modification-date buckets."""

import os
import re
import stat
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from oubi_files import (IMAGE_EXTS, apply_index_changes, direct_image_files, sidecar_path,
                        stage_migrated_indexes)

QUARTER_FOLDER = re.compile(r"[0-9]{4}-Q[1-4]")


@dataclass
class Move:
    source: Path
    target: Path
    images: dict
    sidecar: bool = False


class MigrationError(Exception):
    def __init__(self, code, **values):
        self.code = code
        self.values = values
        super().__init__(code)


def migration_folder(workspace, value):
    root = Path(workspace.images).resolve()
    relative = Path(value)
    path = root / relative
    excluded = Path(workspace.excluded).resolve() if workspace.excluded else None
    if (relative.is_absolute() or any(part.startswith(".") or part == "tags" for part in relative.parts)
            or path.resolve() != path or not path.is_dir()
            or (excluded and path.is_relative_to(excluded))):
        raise ValueError(value)
    return path


def migration_options(workspace, payload):
    if "year" in payload:
        raise MigrationError(408)
    source, destination, recursive = (payload.get(key) for key in ("source", "destination", "recursive"))
    if (type(recursive) is not bool or not isinstance(source, str)
            or (destination is not None and not isinstance(destination, str))
            or payload.get("order") != "modified"):
        raise MigrationError(396)
    try:
        origin = migration_folder(workspace, source)
        target = migration_folder(workspace, destination) if destination is not None else origin
    except (OSError, ValueError, RuntimeError) as error:
        raise MigrationError(397) from error
    return origin, target, recursive


def bucket_for(image):
    modified = datetime.fromtimestamp(image.lstat().st_mtime)
    return f"{modified.year:04d}-Q{(modified.month - 1) // 3 + 1}"


def branch_images(folder, cancelled):
    images = []
    pending = [(folder, True)]
    while pending:
        directory, eligible = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if cancelled():
                    raise InterruptedError
                if entry.is_dir(follow_symlinks=False):
                    if QUARTER_FOLDER.fullmatch(entry.name):
                        return None
                    # Check all descendants before moving the tree, including excluded tag and hidden directories.
                    count_images = eligible and entry.name != "tags" and not entry.name.startswith(".")
                    pending.append((entry.path, count_images))
                elif (eligible and Path(entry.name).suffix.lower() in IMAGE_EXTS
                      and entry.is_file(follow_symlinks=False)):
                    images.append(Path(entry.path))
    return images


def make_plan(workspace, source, destination, recursive, cancelled):
    excluded = Path(workspace.excluded) if workspace.excluded else None
    skipped = []
    moves = []
    buckets = {}
    root = Path(workspace.images).resolve()

    def bucket_path(name):
        if name not in buckets:
            directory = destination / name
            if (source.is_relative_to(directory) or (excluded and excluded.is_relative_to(directory))
                    or (os.path.lexists(directory) and (directory.is_symlink() or not directory.is_dir()))):
                raise MigrationError(398, path=str(directory.relative_to(root)))
            buckets[name] = directory
        return buckets[name]

    def skip(path, code):
        skipped.append({"path": path.relative_to(root).as_posix(), "code": code})

    if any(QUARTER_FOLDER.fullmatch(part) for part in source.relative_to(root).parts):
        skip(source, 409)
        return moves, skipped

    for image in sorted(direct_image_files(source)):
        if cancelled():
            raise InterruptedError
        target = bucket_path(bucket_for(image)) / image.name
        tags = sidecar_path(image)
        target_tags = sidecar_path(target)
        if (tags.parent.is_symlink() or tags.is_symlink()
                or (os.path.lexists(tags) and not tags.is_file())):
            skip(image, 399)
        elif (os.path.lexists(target) or os.path.lexists(target_tags)
                or target_tags.parent.is_symlink()
                or (os.path.lexists(target_tags.parent) and not target_tags.parent.is_dir())):
            skip(image, 392)
        else:
            moves.append(Move(image, target, {image: target}, tags.is_file()))

    if recursive:
        for folder in sorted(source.iterdir()):
            if cancelled():
                raise InterruptedError
            if (folder.name.startswith(".") or folder.name == "tags" or folder.is_symlink()
                    or QUARTER_FOLDER.fullmatch(folder.name) or not folder.is_dir()):
                continue
            if destination != source and destination.is_relative_to(folder):
                continue
            if excluded and excluded.is_relative_to(folder):
                continue
            photos = branch_images(folder, cancelled)
            if cancelled():
                raise InterruptedError
            if photos is None:
                skip(folder, 409)
                continue
            if not photos:
                continue
            quarter, votes = Counter(bucket_for(image) for image in photos).most_common(1)[0]
            if votes * 2 <= len(photos):
                skip(folder, 391)
                continue
            target = bucket_path(quarter) / folder.name
            if os.path.lexists(target):
                skip(folder, 392)
                continue
            moves.append(Move(folder, target, {image: target / image.relative_to(folder) for image in photos}))
    return moves, skipped


def move_into_empty_path(source, target):
    info = source.lstat()
    is_dir = stat.S_ISDIR(info.st_mode)
    if is_dir:
        target.mkdir()
    elif stat.S_ISREG(info.st_mode):
        with target.open("xb"):
            pass
    else:
        raise OSError(f"Expected a regular file or directory: {source.name}")
    reservation = target.lstat()
    try:
        source.rename(target)
    except OSError:
        # Only remove the placeholder we just created.
        if target.lstat() == reservation:
            target.rmdir() if is_dir else target.unlink()
        raise


def migrate(workspace, source, destination, recursive, *, cancelled, progress, before_move):
    plan, skipped = make_plan(workspace, source, destination, recursive, cancelled)
    root = Path(workspace.images).resolve()
    moved = {image: target for move in plan for image, target in move.images.items()}
    if not plan:
        return {}, skipped, False

    completed = []
    created = []

    def mkdir(path):
        if not os.path.lexists(path):
            path.mkdir()
            created.append(path)
        elif path.is_symlink() or not path.is_dir():
            raise OSError(f"Expected a directory: {path.relative_to(root)}")

    def move_path(origin, target):
        move_into_empty_path(origin, target)
        completed.append((origin, target))

    with ExitStack() as cleanup:
        changes = stage_migrated_indexes(root, moved, workspace.csv, workspace.sorted_list, cleanup)
        if cancelled():
            raise InterruptedError
        before_move()
        try:
            for path in sorted({move.target.parent for move in plan}):
                mkdir(path)
            progress(0, len(plan))
            for done, move in enumerate(plan, 1):
                if cancelled():
                    raise InterruptedError
                if move.sidecar:
                    target_tags = sidecar_path(move.target)
                    mkdir(target_tags.parent)
                    move_path(sidecar_path(move.source), target_tags)
                move_path(move.source, move.target)
                progress(done, len(plan))
            if cancelled():
                raise InterruptedError
            cleanup_pending = apply_index_changes(changes)
        except (OSError, ValueError) as error:
            failed_restores = []
            for origin, target in reversed(completed):
                try:
                    move_into_empty_path(target, origin)
                except OSError:
                    failed_restores.append(target.relative_to(root).as_posix())
            for path in reversed(created):
                try:
                    path.rmdir()
                except OSError:
                    continue
            if failed_restores:
                raise MigrationError(400, paths=", ".join(failed_restores)) from error
            raise
    return moved, skipped, cleanup_pending
