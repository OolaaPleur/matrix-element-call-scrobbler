FROM docker.io/library/python:3.14-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    procps \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN python3 -m venv /opt/venv

# requirements.txt installs matrix-bot-common editable from a sibling checkout
# (host setup); the image takes it from GitHub instead.
ARG COMMON_URL=https://github.com/OolaaPleur/matrix-element-call-common/archive/refs/heads/master.tar.gz
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && grep -v '^-e ' /app/requirements.txt > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt "matrix-bot-common @ ${COMMON_URL}"

COPY . /app

RUN useradd -m -u 10001 scrobbler \
    && mkdir -p /app/logs /app/data \
    && chown -R scrobbler:scrobbler /app

USER scrobbler

ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD pgrep -f "python3 main.py" > /dev/null || exit 1

CMD ["python3", "main.py"]
