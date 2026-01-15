# Stage 1: Build Rust native extension
FROM --platform=linux/amd64 python:3.12-slim AS rust-builder

RUN apt-get update && \
    apt-get install -y curl build-essential libssl-dev pkg-config patchelf libgit2-dev && \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.cargo/bin:${PATH}"

RUN pip install maturin

WORKDIR /build
COPY content_services/native /build/native
RUN cd /build/native && maturin build --release


# Stage 2: Runtime image
# Build context: workspace root (driver-ai-app/)
# Usage: docker build -f content_services/Dockerfile -t content-services .

FROM --platform=linux/amd64 python:3.12-slim

ENV PYTHONUNBUFFERED=1

# Install system dependencies (required for pygit2, ghostscript, etc.)
RUN apt-get update && \
    apt-get install -y curl nano ripgrep ghostscript git libgit2-dev && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy uv binary
COPY --from=ghcr.io/astral-sh/uv:0.9.25 /uv /usr/local/bin/uv

WORKDIR /app

# Copy workspace packages FIRST (needed for uv export to resolve workspace deps)
COPY packages/driver_db /packages/driver_db
COPY packages /packages

# Copy workspace config files for uv export
COPY pyproject.toml uv.lock ./
COPY content_services/pyproject.toml content_services/pyproject.toml

# Export only content_services dependencies from unified lockfile
# hatchet-python is the package name in content_services/pyproject.toml
RUN uv export --package hatchet-python --frozen --no-dev --no-emit-workspace \
    -o requirements.txt

# Install external dependencies (workspace packages excluded by --no-emit-workspace)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r requirements.txt

# Copy app source
COPY content_services /app
COPY backend/scripts/ /app/scripts/
COPY content_services/src/inspector/uctags-2024.10.02-linux-x86_64/bin/ctags /ctags

# Copy and install the pre-built Rust wheel from builder stage
COPY --from=rust-builder /build/native/target/wheels/*.whl /tmp/
RUN pip install /tmp/*.whl && rm -rf /tmp/*.whl

COPY content_services/start.sh /start.sh
RUN chmod +x /start.sh

# Copy the setEnv.sh if using the deployment repo
COPY setEnv.sh* /

# Pre-install tiktoken encodings for single-tenant envs without internet access
RUN python -c "import tiktoken; tiktoken.encoding_for_model('gpt-4'); tiktoken.encoding_for_model('gpt-4.1')"

# Capture Git info at build time
ARG GIT_COMMIT
ARG GIT_BRANCH
ENV GIT_COMMIT=${GIT_COMMIT} GIT_BRANCH=${GIT_BRANCH}
ENV HATCHET_ENV=production

# Workspace packages are copied to /packages - add to PYTHONPATH for imports
ENV PYTHONPATH="/packages/driver_db:/packages/shared"

CMD ["/bin/sh", "-c", "/start.sh"]
