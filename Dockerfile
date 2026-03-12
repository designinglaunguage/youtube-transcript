FROM python:3.11-slim

# Install ffmpeg (not a Playwright dep, needed for Instagram audio extraction)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layer 1: Python dependencies (cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Layer 2: Playwright Chromium + all system deps (auto-installs libnss3, libatk, etc.)
# Using --with-deps eliminates manual apt-get for ~15 Chromium libraries
RUN playwright install --with-deps chromium

# Layer 3: Application code (changes most frequently)
COPY . .

# Railway provides PORT env var; default to 8000 for local dev
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
