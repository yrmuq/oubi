#!/bin/sh
set -eu

image="${1:?image}"
platform="${2:?platform}"

# A local model directory exercises the real entrypoint without downloading weights.
container=$(docker run --detach --platform "$platform" --user 1000:1000 -e OUBI_MODEL=/opt/oubi "$image")
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
if ! docker exec -i "$container" python - <<'PY'
import json
import sqlite3
import subprocess
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

base = "http://127.0.0.1:8080"
for attempt in range(30):
    try:
        with urlopen(base + "/api/status", timeout=2) as response:
            status = json.load(response)
        if status["model"] == "ready":
            break
    except (OSError, URLError):
        pass
    time.sleep(1)
else:
    raise SystemExit("Oubi did not start with a ready local model")

assert not status["rocm"]["visible"]
assert status["rocm"]["installed"] == {}
assert status["rocm"]["install"]["state"] == "idle"
assert status["nvidia"]["gpus"] == []
assert status["nvidia"]["installed"] == {}
assert status["nvidia"]["install"]["state"] == "idle"
assert not status["vault_enabled"]

subprocess.run(["id", "-un"], check=True)
subprocess.run(["cryptomator-cli", "--version"], check=True)
subprocess.run(["fusermount3", "--version"], check=True)

from pathlib import Path
from stat import S_IMODE

mount = Path("/mnt/oubi-crypt/mount")
assert S_IMODE(mount.parent.stat().st_mode) == 0o1777
mount.mkdir(mode=0o711)
mount.rmdir()

with urlopen(Request(base, headers={"Accept-Language": "en"}), timeout=2) as response:
    page = response.read()
assert b'<html lang="en">' in page
assert b'id="tag"' in page
assert b'href="/assets/oubi.svg"' in page

with urlopen(base + "/assets/oubi.svg", timeout=2) as response:
    assert response.headers.get_content_type() == "image/svg+xml"
    assert b"<svg" in response.read()
with urlopen(base + "/assets/oubi.webmanifest", timeout=2) as response:
    assert response.headers.get_content_type() == "application/manifest+json"
    icons = json.load(response)["icons"]
for icon in icons:
    with urlopen(base + "/assets/" + icon["src"], timeout=2) as response:
        assert response.headers.get_content_type() == "image/png"
        assert response.read(8) == b"\x89PNG\r\n\x1a\n"

with sqlite3.connect(":memory:") as catalog:
    catalog.execute("CREATE TABLE smoke(path TEXT)")
    catalog.execute("INSERT INTO smoke VALUES ('image.jpg')")
    catalog.commit()
    snapshot = catalog.serialize()
    catalog.deserialize(snapshot)
    assert catalog.execute("SELECT path FROM smoke").fetchone() == ("image.jpg",)
    assert catalog.execute("SELECT value FROM json_each('[1]')").fetchone() == (1,)

from io import BytesIO
from PIL import Image, ImageDraw
from sys import path
from pathlib import Path
from tempfile import TemporaryDirectory
path.insert(0, "/opt/oubi")
from oubi_files import open_image
from oubi_duplicates import MAX_DISTANCE, fingerprint
from oubi_thumbnails import render_jpeg_xl_for_browser, render_thumbnail
from tag_folder import find_pending_images
original = BytesIO()
Image.new("RGB", (2048, 1536)).save(original, "PNG")
preview = render_thumbnail(original.getvalue())
with Image.open(BytesIO(preview)) as image:
    assert image.format == "WEBP" and image.size == (1024, 768)

import pillow_jxl
with TemporaryDirectory() as directory:
    folder = Path(directory)
    file = folder / "picture.JXL"
    Image.new("RGBA", (1200, 900), (32, 64, 96, 127)).save(file, "JXL", lossless=True)
    assert find_pending_images([folder], None, False) == (1, [(1, file)])
    with open_image(file, jpeg_xl=True) as image:
        assert image.convert("RGB").getpixel((0, 0)) == (32, 64, 96)
    with Image.open(BytesIO(render_thumbnail(file.read_bytes(), jpeg_xl=True))) as image:
        assert image.format == "WEBP" and image.size == (1024, 768)
        assert image.getpixel((0, 0))[3] == 127
    with Image.open(BytesIO(render_jpeg_xl_for_browser(file.read_bytes()))) as image:
        assert image.format == "PNG" and image.size == (1200, 900)
        assert image.getpixel((0, 0)) == (32, 64, 96, 127)

    drawing = Image.new("RGB", (480, 360), "#89aacc")
    draw = ImageDraw.Draw(drawing)
    draw.ellipse((31, 50, 264, 275), fill="#ba2233")
    draw.rectangle((217, 106, 434, 333), fill="#efdf66")
    draw.polygon(((301, 41), (407, 194), (193, 251)), fill="#2a575c")
    drawing.save(file, "JXL", lossless=True)
    smaller = folder / "smaller.jpg"
    drawing.resize((240, 180), Image.Resampling.LANCZOS).save(smaller, quality=65)
    assert (fingerprint(file)[0] ^ fingerprint(smaller)[0]).bit_count() <= MAX_DISTANCE

import torch
assert not torch.cuda.is_available(), "CPU image loaded CUDA"
PY
then
    docker logs "$container" >&2
    exit 1
fi
