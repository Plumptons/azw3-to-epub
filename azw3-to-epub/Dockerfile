FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LIBRARY_DIR=/books \
    DELETE_SOURCE=false \
    INITIAL_SCAN=true \
    STABLE_SECONDS=5 \
    POLL_SECONDS=2 \
    RECURSIVE=true \
    LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        calibre \
        python3 \
        python3-pip \
        python3-venv \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY watch_convert.py bindery_client.py storyteller_client.py library_coalesce.py .

ENV PATH="/opt/venv/bin:$PATH"

VOLUME ["/books"]

CMD ["python3", "/app/watch_convert.py"]
