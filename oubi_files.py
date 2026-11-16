"""Paths shared by the tagger, sorter, and web viewer."""

import os
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def sidecar_path(image):
    image = Path(image)
    return image.parent / "tags" / (image.name + ".txt")


def image_files(folder, exclude=None):
    excluded = Path(exclude).resolve() if exclude else None
    for directory, subdirs, names in os.walk(folder):
        parent = Path(directory)
        if excluded and parent.resolve().is_relative_to(excluded):
            subdirs.clear()
            continue
        subdirs[:] = [name for name in subdirs if name != "tags"
                      and not (excluded and (parent / name).resolve() == excluded)]
        for name in names:
            image = parent / name
            if image.suffix.lower() in IMAGE_EXTS and image.is_file() and not (excluded and image.is_symlink()):
                yield image
