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
* Copies files by default. Move and dry-run are switches.
* Reads optional gender corrections from `genders.csv`.

---

## Configuration

<details>
<summary>Environment</summary>

| Input | Action |
| :--- | :--- |
| `OUBI_MODE=all` | Tag, then sort. This is the default. |
| `OUBI_MODE=tag` | Write `tags.csv` and `.txt` sidecars. Do not sort. |
| `OUBI_MODE=sort` | Sort from the existing `tags.csv`. Do not load the model. |
| `OUBI_DRY_RUN=1` | Print the sort plan and do not copy or move. Tagging still writes when the mode is `all`. |
| `OUBI_MOVE=1` | Move files instead of copying them. |
| `OUBI_NSFW_FROM=q` | `rating:q` and `rating:e` go to `nsfw`. `e` sends only `rating:e` to `nsfw`. |
| `OUBI_NO_TXT=1` | Do not write `.txt` sidecars. Resume then has nothing to skip. |
| `OUBI_FORCE=1` | Tag images again even when a `.txt` sidecar exists. |
| `OUBI_COPY_TXT=1` | Copy or move the `.txt` sidecar next to the sorted image. |
| `OUBI_BATCH_SIZE=1` | Images per model pass. Leave this at `1` on CPU. |
| `OUBI_MODEL` | Hugging Face id or a local model directory. |
| `HSA_OVERRIDE_GFX_VERSION` | Kept for a later ROCm image. This CPU image ignores it. |

`OUBI_DRY_RUN` does not stop tagging. To preview a sort, tag first, then set `OUBI_MODE=sort` and `OUBI_DRY_RUN=1`. A dry-run still writes `unknown_genders.txt` when some characters have no gender vote.

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

A push to `dev` publishes `ghcr.io/yrmuq/oubi:dev`. A push to `main` publishes `ghcr.io/yrmuq/oubi:latest`. The `dev` tag never moves `latest`. Each commit is also tagged `sha-<commit>` once, and that tag is not replaced. Only `linux/amd64` is built. There is no numbered release yet. The list of releases is the [GitHub releases](https://github.com/yrmuq/oubi/releases) page.

### Docker

The external network `homelab` must already exist. Create it once with `docker network create homelab` if this host does not have it.

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
    image: ghcr.io/yrmuq/oubi:${OUBI_CHANNEL}
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
      - OUBI_MODE=${OUBI_MODE}
      - OUBI_MODEL=${OUBI_MODEL}
      - OUBI_BATCH_SIZE=${OUBI_BATCH_SIZE}
      - OUBI_NO_TXT=${OUBI_NO_TXT}
      - OUBI_FORCE=${OUBI_FORCE}
      - OUBI_MOVE=${OUBI_MOVE}
      - OUBI_DRY_RUN=${OUBI_DRY_RUN}
      - OUBI_NSFW_FROM=${OUBI_NSFW_FROM}
      - OUBI_COPY_TXT=${OUBI_COPY_TXT}
      - HSA_OVERRIDE_GFX_VERSION=${HSA_OVERRIDE_GFX_VERSION}
    logging:
      driver: json-file
      options:
        max-size: "55m"
        max-file: "1"
    restart: "no"
    networks:
      - homelab

networks:
  homelab:
    external: true
```

`.env`:

```dotenv
TZ=UTC
LOCATION=/path/to/data
PUID=1000
PGID=1000
OUBI_CHANNEL=dev
OUBI_IMAGES=/path/to/pictures
OUBI_OUTPUT=/path/to/sorted
OUBI_MODE=all
OUBI_MODEL=pixai-labs/pixai-tagger-v1.0
OUBI_BATCH_SIZE=1
OUBI_NO_TXT=0
OUBI_FORCE=0
OUBI_MOVE=0
OUBI_DRY_RUN=0
OUBI_NSFW_FROM=q
OUBI_COPY_TXT=0
HSA_OVERRIDE_GFX_VERSION=
```

```bash
docker compose pull
docker compose up
```

`OUBI_CHANNEL=dev` follows the development image. Set `OUBI_CHANNEL=latest` only after that tag has been published from `main`. Pull again to update. The container exits when the job finishes and is not restarted: a second sort would copy the pictures again under new numbers.

For a long run:

```bash
docker compose up -d
docker compose logs -f oubi
```

`tags.csv` stores container paths under `/images`. Keep that mount path. The first tagging run downloads the model weights into the cache volume. They are not in the image.

| Container Path | Purpose |
| :--- | :--- |
| `/images` | Source pictures. Writable, so `.txt` sidecars can be saved next to them. |
| `/output` | Sorted copies or moves. |
| `/data` | `tags.csv`, optional `genders.csv`, and `unknown_genders.txt`. |
| `/cache` | Hugging Face download of the tagger weights. |

To preview the folders before copying, set `OUBI_MODE=sort` and `OUBI_DRY_RUN=1`, then `docker compose up`. Set `OUBI_DRY_RUN=0` and run sort again to copy.

---

## License & Attribution

The scripts and `Dockerfile` in this repository are licensed under `GPL-3.0-only`. See [LICENSE](LICENSE).

| Component | Upstream | License | Integration Details |
| --- | --- | --- | --- |
| PixAI Tagger v1.0 | [pixai-labs/pixai-tagger-v1.0](https://huggingface.co/pixai-labs/pixai-tagger-v1.0) | `Apache-2.0` | Weights download into `/cache` on first tag. Not baked into the image. |
| PyTorch | [pytorch/pytorch](https://github.com/pytorch/pytorch) | `BSD-3-Clause` | CPU build installed in the image. |
| Transformers | [huggingface/transformers](https://github.com/huggingface/transformers) | `Apache-2.0` | Loads the tagger pipeline. |
| timm | [huggingface/pytorch-image-models](https://github.com/huggingface/pytorch-image-models) | `Apache-2.0` | Required by the tagger model code. |
