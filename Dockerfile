FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    PORT=7860

# Install Node.js (for MathJax + Resvg math renderer), CA certs, and DejaVu fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-dejavu-core \
        libstdc++6 \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (UID 1000 compatible with Hugging Face Spaces & secure defaults)
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install lightweight Node.js math renderer dependencies (MathJax 3 + Resvg)
COPY math_renderer/package.json ./math_renderer/
RUN cd /app/math_renderer && npm install --omit=dev --no-audit --no-fund

COPY math_renderer/render.js ./math_renderer/
COPY app ./app
COPY static ./static

RUN mkdir -p /tmp/matplotlib && chown -R appuser:appuser /app /tmp/matplotlib

USER appuser

EXPOSE 7860

# Compatible with SnapDeploy, Render (injects $PORT), and Hugging Face Spaces (defaults to 7860)
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
