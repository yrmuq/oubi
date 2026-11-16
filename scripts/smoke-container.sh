#!/bin/sh
set -eu

image="${1:?image}"
platform="${2:?platform}"

# A local model directory exercises the real entrypoint without downloading weights.
container=$(docker run --detach --platform "$platform" -e OUBI_MODEL=/opt/oubi "$image")
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
if ! docker exec -i "$container" python - <<'PY'
import json
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

subprocess.run(["cryptomator-cli", "--version"], check=True)
subprocess.run(["fusermount3", "--version"], check=True)

with urlopen(Request(base, headers={"Accept-Language": "en"}), timeout=2) as response:
    page = response.read()
assert b'<html lang="en">' in page
assert b'id="tag"' in page

import torch
assert not torch.cuda.is_available(), "CPU image loaded CUDA"
PY
then
    docker logs "$container" >&2
    exit 1
fi
