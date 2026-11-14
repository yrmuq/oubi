<div align="center">

  <h1>oubi</h1>

  [![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
  [![Image](https://img.shields.io/badge/ghcr.io-image-2496ED?logo=docker&logoColor=white)](https://github.com/yrmuq/oubi/pkgs/container/oubi)

</div>

---

> [!NOTE]
> This project is in active development. Behavior can change and bugs may occur. A finished `tags.csv` can become outdated. Delete `tags.csv` and the `.txt` files next to the images, then run tagging again.

---

`oubi` tags a folder of anime images with [PixAI Tagger v1.0](https://huggingface.co/pixai-labs/pixai-tagger-v1.0) and sorts them into folders by series, rating, and character. Tagging and sorting are separate steps, so the sort can be repeated without running the model again. This image runs on CPU.

---

## Features

* Tags every image under the source folder and writes `tags.csv` with columns `file`, `rating`, `character`, `copyright`, `style`, `general`, and `meta`. Tags inside a column are ordered by confidence.
* Skips an image when a `.txt` sidecar is already next to it, so an interrupted run can continue. `--force` retags.
* Sorts from `tags.csv` only, without loading the model, into `<title>/<sfw|nsfw>/<character>/<character>_<title>_0001.jpg`.
* The `dev` image opens a page, downloads the model, and waits. Tagging and sorting start from the buttons on that page.
* Reads optional gender corrections from `genders.csv`.

---

## Configuration

<details>
<summary>Environment</summary>

| Input | Action |
| :--- | :--- |
| `OUBI_MODE` | Ignored on the `dev` image. The page starts tagging and sorting. On `latest`, `all`, `tag`, or `sort` still run at startup and there is no page. |
| `OUBI_PORT` | Host port for the `dev` page. The container listens on `8080`. |
| `OUBI_DRY_RUN=1` | Print the sort plan and do not copy or move. Tagging still writes when the mode is `all`. |
| `OUBI_MOVE=1` | Move files instead of copying them. |
| `OUBI_NSFW_FROM=q` | `rating:q` and `rating:e` go to `nsfw`. `e` sends only `rating:e` to `nsfw`. |
| `OUBI_NO_TXT=1` | Do not write `.txt` sidecars. Resume then has nothing to skip. |
| `OUBI_FORCE=1` | Tag images again even when a `.txt` sidecar exists. |
| `OUBI_COPY_TXT=1` | Copy or move the `.txt` sidecar next to the sorted image. |
| `OUBI_BATCH_SIZE=1` | Images per model pass. Leave this at `1` on CPU. |
| `OUBI_MODEL` | Hugging Face id or a local model directory. |
| `HSA_OVERRIDE_GFX_VERSION` | Kept for a later ROCm image. This CPU image ignores it. |

`OUBI_DRY_RUN` is not used by the page. `Только план раскладки` prints the destination and does not copy. A dry-run still writes `unknown_genders.txt` when some characters have no gender vote.

The container prints the compute device before tagging. On this image the line is `CPU (видеокарта не найдена!)`. That is expected: AMD and Nvidia are not wired up yet. `HSA_OVERRIDE_GFX_VERSION` does not turn a GPU on.

`genders.csv` is optional. Put it in the data volume, with no header. One row is a character tag and `f` or `m`, for example `hatsune_miku,f`. If the file exists, sort loads it. After sort, characters that still have no gender are listed in `unknown_genders.txt` in the same folder. Add those rows to `genders.csv` and run sort again.

</details>

---

## Installation & Usage

### Architecture & Format Support

| Architecture | Status |
| :--- | :---: |
| `x86_64` | ✅ |
| `arm64` | ❌ |

### Release Scheme

A push to `dev` publishes `ghcr.io/yrmuq/oubi:dev`. It does not create a release and it does not move `latest`.

`v0.1.0` is the current stable release on `main`. It has no web page. Its image is `ghcr.io/yrmuq/oubi:latest` until the next merge. The next merge into `main` publishes `v0.2.0`, moves `latest` to that image, and also tags the image with `0.2.0`, which stays put. GitHub opens the release and writes the number back. A second push created by that write does not publish another version.

Each commit is also tagged `sha-<commit>` once, and that tag is not replaced. Only `linux/amd64` is built.

### Docker


Copy `.env.example` to `.env` and set the paths, `PUID`, and `PGID`.

```bash
mkdir -p "$LOCATION/docker/data/oubi/data" "$LOCATION/docker/data/oubi/cache" "$OUBI_OUTPUT"
sudo chown -R "$PUID:$PGID" "$LOCATION/docker/data/oubi" "$OUBI_OUTPUT"
```

The source folder must be writable by that user, because tagging writes a `.txt` file next to each image unless `OUBI_NO_TXT=1`.

`docker-compose.yml`:

```yaml
services:

  oubi:
    container_name: oubi
    image: ghcr.io/yrmuq/oubi:dev
    ports:
      - ${OUBI_PORT}:8080
    user: "${PUID}:${PGID}"
    volumes:
      - ${OUBI_IMAGES}:/images
      - ${OUBI_OUTPUT}:/output
      - ${LOCATION}/docker/data/oubi/data:/data
      - ${LOCATION}/docker/data/oubi/cache:/cache
    environment:
      - TZ=${TZ}
      - HF_HOME=/cache
      - HUGGINGFACE_HUB_CACHE=/cache
      - OUBI_MODEL=pixai-labs/pixai-tagger-v1.0
      - OUBI_BATCH_SIZE=1
      - OUBI_NO_TXT=0
      - OUBI_FORCE=0
      - OUBI_MOVE=0
      - OUBI_DRY_RUN=0
      - OUBI_NSFW_FROM=q
      - OUBI_COPY_TXT=0
      - HF_TOKEN=${HF_TOKEN}
      - HSA_OVERRIDE_GFX_VERSION=${HSA_OVERRIDE_GFX_VERSION}
    logging:
      driver: json-file
      options:
        max-size: "55m"
        max-file: "1"
    restart: "unless-stopped"

```

`.env`:

```dotenv
TZ=UTC
LOCATION=/path/to/data
PUID=1000
PGID=1000
OUBI_IMAGES=/path/to/pictures
OUBI_OUTPUT=/path/to/sorted
OUBI_PORT=8080
HF_TOKEN=
HSA_OVERRIDE_GFX_VERSION=
```

```bash
docker compose pull
docker compose up
```

Open `http://<server>:${OUBI_PORT}`. On startup the `dev` image only downloads the model and serves the page. Tagging and sorting start from the buttons. `Разметить и разложить` does both. `Только план раскладки` does not copy. Running the sort again copies the pictures once more under the next numbers.

`latest` has no page. It still runs the job from `OUBI_MODE` and exits when that job finishes.

For a long run:

```bash
docker compose up -d
docker compose logs -f oubi
```

`tags.csv` stores container paths under `/images`. Keep that mount path. The page downloads the model weights into the cache volume when the container starts. They are not in the image.

| Container Path | Purpose |
| :--- | :--- |
| `/images` | Source pictures. Writable, so `.txt` sidecars can be saved next to them. |
| `/output` | Sorted copies or moves. |
| `/data` | `tags.csv`, optional `genders.csv`, and `unknown_genders.txt`. |
| `/cache` | Hugging Face download of the tagger weights. |

To preview the folders before copying, use `Только план раскладки` on the page.

---

## License & Attribution

The scripts and `Dockerfile` in this repository are licensed under `GPL-3.0-only`. See [LICENSE](LICENSE).

| Component | Upstream | License | Integration Details |
| --- | --- | --- | --- |
| PixAI Tagger v1.0 | [pixai-labs/pixai-tagger-v1.0](https://huggingface.co/pixai-labs/pixai-tagger-v1.0) | `Apache-2.0` | Weights download into `/cache` on first tag. Not baked into the image. |
| PyTorch | [pytorch/pytorch](https://github.com/pytorch/pytorch) | `BSD-3-Clause` | CPU build installed in the image. |
| Transformers | [huggingface/transformers](https://github.com/huggingface/transformers) | `Apache-2.0` | Loads the tagger pipeline. |
| timm | [huggingface/pytorch-image-models](https://github.com/huggingface/pytorch-image-models) | `Apache-2.0` | Required by the tagger model code. |
