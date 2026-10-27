FROM node:22-bookworm-slim AS frontend-build

WORKDIR /workspace/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS lodestar-app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp/lodestar-home \
    DATABASE_URL=sqlite:////app/data/lodestar.db

WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN python -m pip install --no-cache-dir -r backend/requirements.txt

COPY backend/app ./backend/app
COPY --from=frontend-build /workspace/frontend/dist ./frontend/dist

RUN groupadd --system lodestar \
    && useradd --system --gid lodestar --home-dir /nonexistent --shell /usr/sbin/nologin lodestar \
    && install -d -o lodestar -g lodestar /app/data /tmp/lodestar-home

WORKDIR /app/backend
USER lodestar
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
