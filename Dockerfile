FROM python:3.14-slim

ARG APP_UID=1000
ARG APP_GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/rwa

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      ghostscript \
      libreoffice \
      libreoffice-core \
      libreoffice-writer \
      python3-uno \
      antiword \
      poppler-utils \
      libenchant-2-2 \
      tesseract-ocr \
      fonts-liberation \
      fonts-noto-core \
      fonts-dejavu-core \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r /app/requirements.txt

RUN groupadd --gid "${APP_GID}" rwa \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin rwa \
    && mkdir -p /app/_data_main /app/_data_logs /app/_tmp \
    && chown -R rwa:rwa /app /home/rwa

COPY --chown=rwa:rwa src /app/src

USER rwa

CMD ["python", "src/pipeline.py"]
