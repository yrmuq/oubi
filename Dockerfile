# syntax=docker/dockerfile:1.7
FROM debian:bookworm-slim AS cryptomator

ADD --checksum=sha256:6c2ac174f94a2ff30fdfa00ac43669703f1bca1fa633a762dc336bf9d794b1cb https://github.com/cryptomator/cli/releases/download/0.6.2/cryptomator-cli-0.6.2-linux-x64.zip /tmp/cryptomator-cli.zip
RUN apt-get update \
    && apt-get install -y --no-install-recommends unzip \
    && unzip -q /tmp/cryptomator-cli.zip -d /opt

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/cache \
    HUGGINGFACE_HUB_CACHE=/cache \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends fuse3 libdrm-amdgpu1 libelf1 libgomp1 libnuma1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /opt/oubi/requirements.txt
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
        torch==2.15.0+cpu \
        torchvision==0.29.1+cpu \
    && pip install --no-cache-dir -r /opt/oubi/requirements.txt

COPY --from=cryptomator /opt/cryptomator-cli /opt/cryptomator-cli
RUN ln -s /opt/cryptomator-cli/bin/cryptomator-cli /usr/local/bin/cryptomator-cli

COPY tag_folder.py sort_images.py oubi_files.py oubi_cryptomator.py oubi_gpu.py oubi_rocm.py oubi_nvidia.py oubi_web.py oubi_page.html /opt/oubi/
COPY languages /opt/oubi/languages
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh /opt/oubi/tag_folder.py /opt/oubi/sort_images.py /opt/oubi/oubi_web.py

WORKDIR /data
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
