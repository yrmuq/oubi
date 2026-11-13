FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/cache \
    HUGGINGFACE_HUB_CACHE=/cache \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /opt/oubi/requirements.txt
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
        torch==2.15.0+cpu \
        torchvision==0.29.1+cpu \
    && pip install --no-cache-dir -r /opt/oubi/requirements.txt

COPY tag_folder.py sort_images.py /opt/oubi/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh /opt/oubi/tag_folder.py /opt/oubi/sort_images.py

WORKDIR /data
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
