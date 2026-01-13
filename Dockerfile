# Build context: workspace root (driver-ai-app/)
# Usage: docker build -t backend .

FROM python:3.12-slim

WORKDIR /app/

RUN apt-get update && apt-get install -y \
    curl \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

# Copy workspace packages FIRST (needed for uv export to resolve workspace deps)
COPY packages/driver_db /packages/driver_db
COPY packages /packages

# Copy workspace config files for uv export
COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/pyproject.toml

# Export only backend dependencies from unified lockfile
# app is the package name in backend/pyproject.toml
RUN uv export --package app --frozen --no-dev --no-emit-workspace -o requirements.txt

# Install dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r requirements.txt

# Remove build dependencies
RUN apt-get purge -y --auto-remove build-essential \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install start scripts
COPY backend/scripts/start-reload.sh /start-reload.sh
COPY backend/scripts/start.sh /start.sh
COPY backend/scripts/gunicorn_conf.py /gunicorn_conf.py
RUN chmod +x /start-reload.sh /start.sh

# Copy the rest of the application
COPY backend/scripts/ /app/scripts/
COPY backend/prestart.sh /app/
COPY backend/tests-start.sh /app/
COPY backend/app /app/app

# Copy the setEnv.sh if using the deployment repo
COPY setEnv.sh* /

# Install tiktoken encodings for single-tenant envs without internet access
RUN python -c "import tiktoken; tiktoken.encoding_for_model('gpt-4'); tiktoken.encoding_for_model('gpt-4.1')"

# Capture Git info at build time
ARG GIT_COMMIT
ARG GIT_BRANCH
ENV GIT_COMMIT=${GIT_COMMIT} GIT_BRANCH=${GIT_BRANCH}

# Workspace packages are copied to /packages - add to PYTHONPATH for imports
ENV PYTHONPATH="/app:/packages/driver_db:/packages/shared"

CMD [ "/bin/sh", "-c", "exec /start.sh" ]
