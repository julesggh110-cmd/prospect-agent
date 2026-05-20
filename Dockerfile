# prospect-agent — runtime image
# ------------------------------------------------------------
# Multi-stage build:
#   - builder  : compile wheels with build deps, then discard
#   - runtime  : slim image with only the wheels + app code
# Result: ~120-150 MB final image, fast cold start.
#
# Build:
#   docker build -t prospect-agent .
#
# Run (interactive, with a mounted output dir):
#   docker run --rm -it \
#     -v $(pwd)/output:/app/output \
#     -v $(pwd)/data:/app/data \
#     --env-file .env \
#     prospect-agent \
#     --naf 56.10A --departement 31 --tranche-effectif 11 \
#     --volume 20 --output toulouse-restos

# ---- Stage 1: builder ----
FROM python:3.12-slim AS builder

# Build deps for native extensions (selectolax, lxml-like wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only the dep manifests first → leverage Docker layer cache
COPY pyproject.toml requirements.txt ./

# Build wheels for all deps into a local cache
RUN pip wheel --no-cache-dir --wheel-dir=/wheels -r requirements.txt


# ---- Stage 2: runtime ----
FROM python:3.12-slim AS runtime

# Runtime-only system deps:
#   - ca-certificates: TLS verification
#   - curl: useful for HEALTHCHECK + ad-hoc debugging
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy wheels from the builder stage and install
COPY --from=builder /wheels /wheels
COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# Copy app code (do this AFTER deps install so code-only changes don't
# invalidate the dep layer cache)
COPY *.py ./
COPY SKILL.md README.md DETAILS.md ROADMAP.md ./

# Output/data dirs (mount as volumes at runtime)
RUN mkdir -p /app/output /app/data/cache

# Reasonable defaults — overridable via `docker run --env`
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Paris

# Default entrypoint = the unified CLI. Override with `docker run prospect-agent python xxx.py`
ENTRYPOINT ["python", "run_campaign.py"]

# `docker run prospect-agent --help` lists available flags.
CMD ["--help"]
