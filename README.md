<div align="center">

  <h1>oubi</h1>

  [![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
  [![Image](https://img.shields.io/badge/ghcr.io-image-2496ED?logo=docker&logoColor=white)](https://github.com/yrmuq/oubi/pkgs/container/oubi)

</div>

---

> [!NOTE]
> This project is in active development. Behavior can change and bugs may occur. A finished `tags.csv` can become outdated. Delete `tags.csv` and the `.txt` files next to the images, then run tagging again.

---

`oubi` tags anime images with [PixAI Tagger v1.0](https://huggingface.co/pixai-labs/pixai-tagger-v1.0) and sorts them by series, rating, character, and selected genres. The self-hosted web interface runs in a CPU container. Tagging and sorting are separate steps, so you can sort again using saved tags.

![Oubi web interface showing the folder list, image preview, tag filter, and job controls](docs/oubi-web.png)

---

## Features

- Select source folders, identify images, and preview saved tags in the browser.
- Resume tagging from existing `.txt` sidecars. Set `OUBI_FORCE=1` to tag those images again.
- Plan a sort from `tags.csv`, then copy or move images. Numbered pages stay in their series and original art can be sorted by configured genres.
- Filter the preview by tags, correct character genders through `genders.csv`, and customize the layout, theme, and language.

---

<details>
<summary><h2>Configuration</h2></summary>

| Input | Action |
| :--- | :--- |
| `Identify` | Tag images in the selected folders when the model is ready. |
| `Sort` | Copy the selected folders' tagged images into their destinations. Set `OUBI_MOVE=1` to move them. |
| `Sort plan` | Show destination paths before copying or moving. A plan may write `unknown_genders.txt`. |
| `+` / `−` | Add a genre field or remove the last one, keeping at least one field available. |
| `OUBI_PORT` | Choose the host port. The container listens on `8080`. |
| `OUBI_NO_TXT=1` | Skip writing tag sidecars. Resume relies on sidecars that already exist. |
| `OUBI_NSFW_FROM` | Use `q` to include `rating:q` and `rating:e`, or `e` to include only `rating:e`. |
| `OUBI_COPY_TXT=1` | Copy or move accompanying `.txt` files with sorted images. |
| `OUBI_BATCH_SIZE` | Set the number of images in a model pass. The CPU default is `1`. |
| `OUBI_MODEL` | Use a Hugging Face model ID or a model directory visible to the container. |
| `OUBI_GENRE_1`–`OUBI_GENRE_20` | Set initial genre tags in order. A saved genre list in the web interface takes precedence. |

Put optional gender overrides in `genders.csv` under the data directory as headerless rows like `hatsune_miku,f`. Copy entries from `unknown_genders.txt` into that file, add `f` or `m`, then sort again.

</details>

---

## Installation & Usage

### Architecture & Format Support

| Architecture | Status |
| :--- | :---: |
| `x86_64` | ✅ |
| `arm64` | ❌ |

The published container supports `linux/amd64` and uses CPU inference.

### Release Scheme

Use the [latest stable release](https://github.com/yrmuq/oubi/releases/latest) for regular use and the [release list](https://github.com/yrmuq/oubi/releases) for versioned images. Stable releases are `X.Y.0` and publish `ghcr.io/yrmuq/oubi:latest`. Automated dependency prereleases use `X.Y.Z` with `Z > 0` and use their numbered tag. Development builds use `:dev`. All three channels run the web interface.

### Docker

Put the [Compose file](docker-compose.yml) and the [environment example](.env.example) in the same folder. Copy the example to `.env` and fill in your image and output paths, `LOCATION`, `PUID`, and `PGID`. The example uses `OUBI_CHANNEL=dev`. Set `OUBI_CHANNEL=latest` for the stable image.

Create the host directories for `/data`, `/cache`, and `/output`, and give your configured container user write access. The user also needs write access to the source folder when writing sidecars or moving originals.

Run these commands for installation and updates.

```bash
docker compose pull
docker compose up -d
```

Open the server address on `OUBI_PORT` (default `8080`), choose source folders, and use `Identify`, `Sort plan`, or `Sort`. The application downloads model weights into `/cache` on startup. If the selected model requires Hugging Face authentication, set `HF_TOKEN` in your local `.env`.

| Container Path | Purpose |
| :--- | :--- |
| `/images` | Source images and optional tag sidecars. Keep this mount path because saved indexes use it. |
| `/output` | Sorted images. |
| `/data` | Saved tags, sorting history, gender overrides, and web settings. |
| `/cache` | Downloaded model weights, kept across image updates. |

---

## License & Attribution

The scripts and `Dockerfile` in this repository are licensed under `GPL-3.0-only`. See [LICENSE](LICENSE).

| Component | Upstream | License | Integration Details |
| --- | --- | --- | --- |
| PixAI Tagger v1.0 | [pixai-labs/pixai-tagger-v1.0](https://huggingface.co/pixai-labs/pixai-tagger-v1.0) | `Apache-2.0` | Weights download into `/cache` when the app starts. |
| PyTorch | [pytorch/pytorch](https://github.com/pytorch/pytorch) | `BSD-3-Clause` | CPU build installed in the image. |
| Transformers | [huggingface/transformers](https://github.com/huggingface/transformers) | `Apache-2.0` | Loads the tagger pipeline. |
| timm | [huggingface/pytorch-image-models](https://github.com/huggingface/pytorch-image-models) | `Apache-2.0` | Required by the tagger model code. |
