"""Small WebP previews stored under the mounted vault's private data directory."""
import hashlib
import io
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

from oubi_files import open_image

DIRECTORY = "thumbnails"
PREVIEW_SIZE = 1024
CACHE_FORMAT = "webp-1024-q82-v1"
CACHED_NAME = re.compile(r"([a-f0-9]{64})\.[a-f0-9]{64}\.webp")


def digest(value):
    return hashlib.sha256(value.encode("utf-8", errors="surrogateescape")).hexdigest()


def render_thumbnail(source, *, jpeg_xl=False):
    return _render_image(source, "WEBP", jpeg_xl=jpeg_xl)


def render_jpeg_xl_for_browser(source):
    return _render_image(source, "PNG", jpeg_xl=True)


def _render_image(source, format_, *, jpeg_xl):
    from PIL import Image, ImageOps

    try:
        with open_image(io.BytesIO(source), jpeg_xl=jpeg_xl) as original:
            if not jpeg_xl and getattr(original, "is_animated", False):
                return None
            # Apply the camera orientation before encoding the preview.
            image = ImageOps.exif_transpose(original)
            if format_ == "WEBP":
                image.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE))
            modes = ("RGB", "RGBA") if format_ == "WEBP" else ("RGB", "RGBA", "L", "LA", "I;16")
            if image.mode not in modes:
                image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
            output = io.BytesIO()
            options = {"quality": 82} if format_ == "WEBP" else {}
            image.save(output, format_, **options)
            return output.getvalue()
    except (OSError, ValueError, RuntimeError, Image.DecompressionBombError):
        return None


class VaultThumbnails:
    def __init__(self, data):
        self.root = Path(data) / DIRECTORY

    def bucket(self, key):
        folder = self.root / key[:2]
        if self.root.is_symlink() or folder.is_symlink():
            raise OSError("Invalid thumbnail directory")
        return folder

    def filename(self, key, revision):
        return key + "." + digest(CACHE_FORMAT + revision) + ".webp"

    def read(self, relative, revision):
        key = digest(relative)
        path = self.bucket(key) / self.filename(key, revision)
        if path.is_symlink():
            return None
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def write(self, relative, revision, thumbnail):
        key = digest(relative)
        folder = self.bucket(key)
        self.root.mkdir(mode=0o700, exist_ok=True)
        folder.mkdir(mode=0o700, exist_ok=True)
        name = self.filename(key, revision)
        with NamedTemporaryFile(dir=folder, prefix=".thumbnail-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(thumbnail)
                handle.close()
                temporary.replace(folder / name)
            finally:
                temporary.unlink(missing_ok=True)
        self._remove_versions(folder, key, keep=name)

    def remove(self, relative):
        key = digest(relative)
        self._remove_versions(self.bucket(key), key)

    def _remove_versions(self, folder, key, *, keep=None):
        if not folder.is_dir():
            return
        for file in folder.iterdir():
            matched = CACHED_NAME.fullmatch(file.name)
            if matched and matched[1] == key and file.name != keep:
                file.unlink(missing_ok=True)

    def prune(self, images):
        if self.root.is_symlink() or not self.root.is_dir():
            return
        keys = {digest(relative) for relative in images}
        for folder in self.root.iterdir():
            if not re.fullmatch("[a-f0-9]{2}", folder.name) or folder.is_symlink() or not folder.is_dir():
                continue
            for file in folder.iterdir():
                matched = CACHED_NAME.fullmatch(file.name)
                if matched and matched[1] not in keys:
                    file.unlink(missing_ok=True)
