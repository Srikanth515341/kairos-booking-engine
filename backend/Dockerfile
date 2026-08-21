# Celery worker/beat image (Implementation Plan Phase 13). Not used for
# the API service — that still runs via `manage.py runserver` on the host
# per README.md "Running Locally"; containerizing it is Rollout's job
# (Phase 30). This exists so the worker and beat services in
# infra/docker-compose.yml have something to build and run.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY kairos ./kairos

RUN pip install --no-cache-dir -e .

# Overridden per-service in docker-compose.yml (worker vs. beat).
CMD ["celery", "-A", "kairos", "worker", "--loglevel=info"]
