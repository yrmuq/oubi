# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS python-base
FROM python-base AS cryptomator

RUN --mount=type=bind,source=dependencies/os-refresh.txt,target=/tmp/os-refresh.txt apt-get update \
    && apt-get install -y --no-install-recommends unzip
COPY scripts/cryptomator-cli.py dependencies/cryptomator.json /tmp/
RUN python /tmp/cryptomator-cli.py download /tmp/cryptomator.json /tmp/cryptomator-cli.zip \
    && unzip -q /tmp/cryptomator-cli.zip -d /opt

FROM python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/cache \
    HUGGINGFACE_HUB_CACHE=/cache \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN --mount=type=bind,source=dependencies/os-refresh.txt,target=/tmp/os-refresh.txt apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends fuse3 libdrm-amdgpu1 libelf1 libgomp1 libnuma1 \
        systemd libtss2-esys-3.0.2-0 libtss2-rc0 libtss2-mu0 libtss2-tcti-device0 tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY dependencies/pip.txt dependencies/cpu.lock.txt /tmp/
RUN pip install --no-cache-dir --only-binary=:all: --no-deps -r /tmp/pip.txt \
    && pip install --no-cache-dir --only-binary=:all: --require-hashes --no-deps \
        --extra-index-url https://download.pytorch.org/whl/cpu -r /tmp/cpu.lock.txt \
    && pip check

# The tagger loads the shared codec from its script directory in CPU and GPU environments.
COPY dependencies/jxl.txt /tmp/jxl.txt
RUN pip install --no-cache-dir --no-deps --only-binary=:all: --target /opt/oubi -r /tmp/jxl.txt

COPY requirements.txt /opt/oubi/requirements.txt
COPY dependencies/gpu.json dependencies/pip.txt dependencies/shared.txt dependencies/default-model.txt /opt/oubi/dependencies/

COPY --from=cryptomator /opt/cryptomator-cli /opt/cryptomator-cli
RUN ln -s /opt/cryptomator-cli/bin/cryptomator-cli /usr/local/bin/cryptomator-cli
RUN install -d -m 1777 /mnt/oubi-crypt

# fusermount3 resolves the default Compose UID through the user database.
RUN if ! getent group 1000 >/dev/null; then groupadd --gid 1000 oubi; fi \
    && if ! getent passwd 1000 >/dev/null; then \
        useradd --uid 1000 --gid 1000 --no-create-home --no-log-init --home-dir /data --shell /usr/sbin/nologin oubi; \
    fi

COPY tag_folder.py sort_images.py oubi_files.py oubi_index.py oubi_duplicates.py oubi_thumbnails.py oubi_cryptomator.py oubi_gpu.py oubi_rocm.py oubi_nvidia.py oubi_schedule.py oubi_migration.py oubi_web.py oubi_page.html /opt/oubi/
COPY assets /opt/oubi/assets
COPY languages /opt/oubi/languages
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh /opt/oubi/tag_folder.py /opt/oubi/sort_images.py /opt/oubi/oubi_web.py

WORKDIR /data
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
