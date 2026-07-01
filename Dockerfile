# Single-stage image for the Twilio ↔ OpenAI Realtime bridge.
# Kept minimal on purpose — per project guidance, don't gold-plate infra.
FROM python:3.13-slim

WORKDIR /app

# System deps: none needed at runtime (certifi bundles CA certs; websockets/uvicorn are pure Python
# or ship wheels). If we ever add asyncpg/psycopg or similar, add build-essential/libpq-dev here.

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Copy the app. Keep this last so code changes don't bust the deps layer.
COPY main.py .

# Fly's internal port. main.py reads PORT from env; fly.toml's internal_port must match this.
ENV PORT=8080
EXPOSE 8080

# uvicorn is preferred as PID 1 (clean SIGTERM handling on Fly's rolling deploys).
# --host 0.0.0.0 so the container is reachable from outside the container network.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
