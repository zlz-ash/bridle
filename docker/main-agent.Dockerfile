FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/bridle
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/src backend/src
RUN pip install --no-cache-dir -e backend

RUN useradd -u 1000 -ms /bin/bash bridle
USER bridle
ENTRYPOINT ["bridle-main-agent"]
