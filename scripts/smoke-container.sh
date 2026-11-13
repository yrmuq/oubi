#!/bin/sh
set -eu

# ===== CHECK THE CPU IMAGE STARTS =====
image="${1:?image}"
platform="${2:?platform}"

docker run --rm --platform "$platform" --entrypoint python "$image" -c 'import pathlib, torch
assert torch.cuda.is_available() is False, "CPU image loaded CUDA"
for path in ("/usr/local/bin/entrypoint.sh", "/opt/oubi/tag_folder.py", "/opt/oubi/sort_images.py"):
    assert pathlib.Path(path).is_file(), path
print(torch.__version__)'

if docker run --rm --platform "$platform" "$image"; then
  echo "The image started without its pictures volume." >&2
  exit 1
fi
