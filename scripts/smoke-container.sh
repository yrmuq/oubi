#!/bin/sh
set -eu

# ===== CHECK THE CPU IMAGE STARTS =====
image="${1:?image}"
platform="${2:?platform}"

docker run --rm --platform "$platform" --entrypoint python "$image" -c 'import pathlib, sys, torch
sys.path.insert(0, "/opt/oubi")
import oubi_web
assert torch.cuda.is_available() is False, "CPU image loaded CUDA"
for path in ("/usr/local/bin/entrypoint.sh", "/opt/oubi/tag_folder.py", "/opt/oubi/sort_images.py", "/opt/oubi/oubi_web.py"):
    assert pathlib.Path(path).is_file(), path
found = oubi_web.parse_progress("Скачивание: 25% (1.0 КБ из 4.0 КБ), сейчас 1.0 КБ/с")
assert found["percent"] == 25, found
print(torch.__version__)'
