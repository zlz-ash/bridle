FROM python:3.12-slim-bookworm@sha256:db8e83a44af476c636a6a753adace39ad37863b63c0afd2862db7bbafeeb3944

ARG WORKER_IMAGE_DIGEST=unknown
ARG PRODUCER_VERSION=bridle.worker/v1
ARG PYTEST_VERSION=8.3.5
ARG PYTEST_ASYNCIO_VERSION=0.25.3

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml /tmp/bridle-backend/pyproject.toml
COPY backend/src /tmp/bridle-backend/src

RUN pip install --no-cache-dir "pytest==${PYTEST_VERSION}" "pytest-asyncio==${PYTEST_ASYNCIO_VERSION}" \
    && pip install --no-cache-dir -e /tmp/bridle-backend

RUN groupadd --gid 1000 worker \
    && useradd --create-home --uid 1000 --gid 1000 worker

LABEL bridle.worker_image_digest="${WORKER_IMAGE_DIGEST}"
LABEL bridle.producer_version="${PRODUCER_VERSION}"
LABEL bridle.pytest_version="${PYTEST_VERSION}"
LABEL bridle.pytest_asyncio_version="${PYTEST_ASYNCIO_VERSION}"
LABEL bridle.base_image_digest="sha256:db8e83a44af476c636a6a753adace39ad37863b63c0afd2862db7bbafeeb3944"

USER worker
WORKDIR /home/worker

CMD ["python", "-m", "pytest", "--version"]
