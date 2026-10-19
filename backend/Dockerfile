# Homework 2 API image. Build context is the Test2 repository root.
# Do not copy .env into this image. Model keys are runtime environment only.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SEED_DEMO=1 \
    HW2_DATA_DIR=/data \
    HW2_CONFIG_DIR=/app/config \
    PATH="/app/backend/.venv/bin:${PATH}"

WORKDIR /app/backend

# Dependency layer. --all-groups includes pytest, which the verifier tool
# invokes as `python -m pytest`. The scripted demo seed itself does not need it.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --all-groups

COPY backend/app ./app
COPY backend/scripts ./scripts
COPY backend/tests/fixtures ./tests/fixtures
COPY config /app/config

# Editable install so prompt files and config paths stay next to the source.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-groups \
    && test -f /app/config/agents.yaml \
    && test -f /app/config/workflow.sequential.yaml \
    && /app/backend/.venv/bin/python -c "import sqlite3; sqlite3.connect(':memory:').close(); from app.registry.agents import load_prompt; [load_prompt(name) for name in ('parent', 'reviewer', 'fixer', 'verifier', 'test_generator')]; import scripts.seed_demo"

COPY <<'EOF' /usr/local/bin/hw2-entrypoint.sh
#!/bin/sh
set -eu
mkdir -p /data
cd /app/backend
if [ "${SEED_DEMO:-0}" = "1" ]; then
  if [ -f /data/.hw2-demo-seeded ]; then
    echo "hw2 demo: existing demo data found in /data; skipping seed"
  else
    echo "hw2 demo: seeding scripted review runs into /data"
    echo "hw2 demo: HW2_MODEL_API_KEY is not required for this seed"
    if ! /app/backend/.venv/bin/python -m scripts.seed_demo --data-dir /data; then
      echo "hw2 demo: seed failed. If /data is only a partial seed, run 'docker compose down -v' and then 'make demo' again." >&2
      exit 1
    fi
    touch /data/.hw2-demo-seeded
    echo "hw2 demo: seed finished"
  fi
fi
exec /app/backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
EOF

EXPOSE 8000
ENTRYPOINT ["sh", "/usr/local/bin/hw2-entrypoint.sh"]
