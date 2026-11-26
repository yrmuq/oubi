#!/bin/sh
set -eu

uv pip compile requirements.txt dependencies/cpu-wheels.txt --upgrade \
    --no-header --no-annotate --generate-hashes --only-binary=:all: \
    --python-version 3.12 --python-platform x86_64-manylinux_2_36 \
    --index https://download.pytorch.org/whl/cpu --default-index https://pypi.org/simple \
    --index-strategy unsafe-best-match --output-file dependencies/cpu.lock.txt >/dev/null

awk '/^[[:alnum:]][[:alnum:]._-]*==/ && !/^torch(vision)?==/ {print $1}' \
    dependencies/cpu.lock.txt > dependencies/shared.txt
