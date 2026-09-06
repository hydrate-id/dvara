# syntax=docker/dockerfile:1

# Camoufox browser version must match the Python lib in pyproject.toml.
# Bump both when `camoufox fetch` installs a newer version during dev.
ARG CAMOUFOX_VERSION=152.0.4-beta.30
ARG CAMOUFOX_ARCH=x86_64

FROM python:3.12-slim

ARG CAMOUFOX_VERSION
ARG CAMOUFOX_ARCH

ENV PYTHONUNBUFFERED=1
ENV DVARA_DB=/data/dvara.db
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

# uv installs dependencies fast
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY dvara ./dvara
RUN uv sync --frozen --no-dev

# Firefox runtime libraries for the distro (installed via playwright; run after sync)
RUN uv run python -m playwright install-deps firefox

# Install the Camoufox browser binary manually into ~/.cache/camoufox.
# Avoids `camoufox fetch`, which can hit the GitHub API rate limit at build time.
RUN set -eux; \
    V="${CAMOUFOX_VERSION}"; \
    VER="${V%%-*}"; BUILD="${V#*-}"; \
    asset="camoufox-${V}-lin.${CAMOUFOX_ARCH}.zip"; \
    curl -fsSL "https://github.com/daijro/camoufox/releases/download/v${V}/${asset}" -o /tmp/cf.zip; \
    dir="/root/.cache/camoufox/browsers/official/${V}"; \
    mkdir -p "$dir"; \
    python -m zipfile -e /tmp/cf.zip "$dir"; \
    rm /tmp/cf.zip; \
    chmod -R 755 "$dir"; \
    if [ ! -x "$dir/camoufox-bin" ] && [ -x "$dir/browser/camoufox-bin" ]; then \
        ln -s browser/camoufox-bin "$dir/camoufox-bin"; \
    fi; \
    printf '{"version":"%s","build":"%s","prerelease":true}' "$VER" "$BUILD" > "$dir/version.json"; \
    printf '{"active_version":"browsers/official/%s"}' "$V" > /root/.cache/camoufox/config.json; \
    touch /root/.cache/camoufox/.0.5_FLAG

VOLUME /data
EXPOSE 8080

CMD ["uv", "run", "uvicorn", "dvara.main:app", "--host", "0.0.0.0", "--port", "8080"]
