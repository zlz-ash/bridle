FROM python:3.12-slim-bookworm

ARG REVIEW_SOURCE_DIGEST=unknown
ARG PRODUCER_VERSION=bridle.entrypoint/v1

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /opt/bridle

COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/src backend/src

RUN pip install --no-cache-dir -e backend pytest>=8.0

RUN printf '%s\n' \
  "{\"schema\":\"bridle.review_image_metadata/v1\",\"source_digest\":\"${REVIEW_SOURCE_DIGEST}\",\"producer\":\"${PRODUCER_VERSION}\"}" \
  > /opt/bridle/.review-metadata.json

LABEL bridle.review_source_digest="${REVIEW_SOURCE_DIGEST}"
LABEL bridle.producer_version="${PRODUCER_VERSION}"

RUN useradd --create-home --uid 1000 bridle

USER bridle
WORKDIR /container

CMD ["python", "-m", "bridle.agent.container.entrypoint", "--keep-alive"]
