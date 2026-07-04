FROM python:3.12-slim-bookworm

ARG WORKER_IMAGE_DIGEST=unknown
ARG PRODUCER_VERSION=bridle.worker/v1
ARG PYTEST_VERSION=8.3.5

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "pytest==${PYTEST_VERSION}" "pytest-asyncio>=0.25"

RUN groupadd --gid 1000 worker \
    && useradd --create-home --uid 1000 --gid 1000 worker

LABEL bridle.worker_image_digest="${WORKER_IMAGE_DIGEST}"
LABEL bridle.producer_version="${PRODUCER_VERSION}"
LABEL bridle.pytest_version="${PYTEST_VERSION}"

USER worker
WORKDIR /home/worker

CMD ["python", "-m", "pytest", "--version"]
