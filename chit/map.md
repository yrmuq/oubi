# Oubi repository map

## Application

- [oubi_web.py](../oubi_web.py) serves the browser page and JSON API using Python `http.server`. `Handler` routes requests and `App` manages model download, folder counts, job subprocesses, GPU installation confirmation, and progress. `tag_command` and `sort_command` connect the API to the CLI scripts.
- [oubi_page.html](../oubi_page.html) contains the HTML, CSS, and JavaScript for folder selection, image preview, tag filters, job controls, and the draggable layout. It receives startup state from `page_bytes` and uses the `/api/*` routes to refresh or save it.
- [oubi_gpu.py](../oubi_gpu.py) manages optional Python environments, installation progress, and GPU smoke tests. [oubi_rocm.py](../oubi_rocm.py) detects AMD architectures through Linux KFD and selects ROCm packages. [oubi_nvidia.py](../oubi_nvidia.py) lists NVIDIA cards through `nvidia-smi` and selects CUDA packages by compute capability and driver.
- [tag_folder.py](../tag_folder.py) downloads and caches PixAI Tagger through Hugging Face Hub, runs inference through Transformers and PyTorch, and writes `tags.csv` and optional `.txt` sidecars.
- [sort_images.py](../sort_images.py) reads `tags.csv` and optional `genders.csv`, groups series, chooses genre and character destinations, and plans, copies, or moves images. It does not load the tagger.
- [languages/](../languages/) provides the English and Russian catalogs. `oubi_web.py` loads and validates the catalogs for the browser. Both CLI scripts use its `text` function for log messages.

## Deployment and packaging

- [Dockerfile](../Dockerfile), [requirements.txt](../requirements.txt), and [entrypoint.sh](../entrypoint.sh) define the CPU image and web startup.
- [docker-compose.yml](../docker-compose.yml) and [.env.example](../.env.example) define image channel selection, host paths, and environment options. The optional [AMD override](../docker-compose.amd.yml) and [NVIDIA override](../docker-compose.nvidia.yml) grant access to host GPUs. `/images` holds source files, `/output` holds sorted results, `/data` holds tag indexes, UI settings, and optional GPU environments, and `/cache` holds model files.
- [image.yml](../.github/workflows/image.yml) checks and publishes the dev image. [release.yml](../.github/workflows/release.yml) computes the stable version and records the release. Both call [publish-image.yml](../.github/workflows/publish-image.yml) to build, smoke-test, and publish to GHCR.
- [dependabot.yml](../.github/dependabot.yml) groups GitHub Actions, Docker, and pip updates. [deps.yml](../.github/workflows/deps.yml) invokes [sync-deps.sh](../scripts/sync-deps.sh) and [update-torch-pins.py](../scripts/update-torch-pins.py) to maintain the dependency branch and publish tested prereleases.
- [VERSION](../VERSION) and [bump-version.sh](../scripts/bump-version.sh) track and allocate release versions. [publish-image.sh](../scripts/publish-image.sh) verifies image metadata and updates GitHub Releases and GHCR aliases. [smoke-container.sh](../scripts/smoke-container.sh) checks startup through the container entrypoint.

## Tests

- [tests/](../tests/) covers the tagger helpers, sorter rules, web API helpers, persistent UI settings, and version increments. Run [check.sh](../scripts/check.sh) for the full local suite.
