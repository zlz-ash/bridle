FROM python:3.12-slim-bookworm@sha256:db8e83a44af476c636a6a753adace39ad37863b63c0afd2862db7bbafeeb3944

ARG REVIEW_SOURCE_DIGEST=unknown
ARG PRODUCER_VERSION=bridle.entrypoint/v1
ARG PYTEST_VERSION=8.3.5
ARG PYTEST_ASYNCIO_VERSION=0.25.3

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /opt/bridle

COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/src backend/src

RUN pip install --no-cache-dir -e backend "pytest==${PYTEST_VERSION}" "pytest-asyncio==${PYTEST_ASYNCIO_VERSION}"

RUN printf '%s\n' \
  "{\"schema\":\"bridle.review_image_metadata/v1\",\"source_digest\":\"${REVIEW_SOURCE_DIGEST}\",\"producer\":\"${PRODUCER_VERSION}\"}" \
  > /opt/bridle/.review-metadata.json

LABEL bridle.review_source_digest="${REVIEW_SOURCE_DIGEST}"
LABEL bridle.producer_version="${PRODUCER_VERSION}"
LABEL bridle.pytest_version="${PYTEST_VERSION}"
LABEL bridle.pytest_asyncio_version="${PYTEST_ASYNCIO_VERSION}"
LABEL bridle.base_image_digest="sha256:db8e83a44af476c636a6a753adace39ad37863b63c0afd2862db7bbafeeb3944"

RUN useradd --create-home --uid 1000 bridle

USER bridle
WORKDIR /container

CMD ["python", "-m", "bridle.agent.container.entrypoint", "--keep-alive"]
