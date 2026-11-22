# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV DVARA_DB=/data/dvara.db
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

# uv installs dependencies fast
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY dvara ./dvara
RUN uv sync --frozen --no-dev

# Chromium (patchright build, used by Scrapling) plus its system libraries.
RUN uv run patchright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

VOLUME /data
EXPOSE 8080

CMD ["uv", "run", "uvicorn", "dvara.main:app", "--host", "0.0.0.0", "--port", "8080"]
