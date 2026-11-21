# Stage 1: Install lightweight Node.js math renderer dependencies (MathJax 3 + Resvg)
FROM node:20-bookworm-slim AS math-builder

WORKDIR /build/math_renderer
COPY math_renderer/package.json ./
RUN npm install --omit=dev --no-audit --no-fund

# Stage 2: Python FastAPI + embedded Node.js binary runtime
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    PORT=7860

# Minimal system packages: CA certs for HTTPS/SMTP TLS, libstdc++6 for Node/Resvg,
# and DejaVu fonts for rare non-ASCII characters inside \text{...}
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-dejavu-core \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# Copy Node.js binary from matching Debian bookworm stage
COPY --from=math-builder /usr/local/bin/node /usr/local/bin/node

# Non-root user (UID 1000 is required by Hugging Face Spaces)
RUN useradd -m -u 1000 appuser

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=math-builder /build/math_renderer/node_modules ./math_renderer/node_modules
COPY math_renderer/package.json math_renderer/render.js ./math_renderer/
COPY app ./app
COPY static ./static

RUN mkdir -p /tmp/matplotlib && chown -R appuser:appuser /app /tmp/matplotlib

USER appuser

EXPOSE 7860

# Compatible with both Render (injects $PORT) and Hugging Face Spaces (defaults to 7860)
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
