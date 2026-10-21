# PersonalAgent Containerized Environment
FROM python:3.13.7-slim-bookworm

# Avoid prompts during apt installs
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install runtime system packages:
# - ffmpeg: required for video rendering, clipping, and subtitles
# - libgl1, libglib2.0-0: required for OpenCV and MediaPipe face tracking
# - ca-certificates, curl: required for secure API requests and health verification
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency specifications first for optimal Docker layer caching
COPY requirements.txt .

# Upgrade pip and install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure storage directories exist with appropriate permissions
RUN mkdir -p data review logs work

# Copy Render runtime secret files into the paths used by the application,
# then start the Telegram control panel bot. Render's Docker Command can stay blank.
CMD ["/bin/sh", "/app/docker-entrypoint.sh"]
