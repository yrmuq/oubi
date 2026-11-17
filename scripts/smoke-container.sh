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
from PIL import Image
from sys import path
path.insert(0, "/opt/oubi")
from oubi_thumbnails import render_thumbnail
original = BytesIO()
Image.new("RGB", (2048, 1536)).save(original, "PNG")
preview = render_thumbnail(original.getvalue())
with Image.open(BytesIO(preview)) as image:
    assert image.format == "WEBP" and image.size == (1024, 768)

import torch
assert not torch.cuda.is_available(), "CPU image loaded CUDA"
PY
then
    docker logs "$container" >&2
    exit 1
fi
