#!/bin/sh
set -eu

python -m unittest discover -s tests
sh tests/bump-version.test.sh
sh -n entrypoint.sh
sh -n scripts/smoke-container.sh
sh -n scripts/sync-deps.sh
bash -n scripts/publish-image.sh
