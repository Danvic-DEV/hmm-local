FROM node:20-slim AS ui-builder

WORKDIR /ui-react

# Install UI dependencies first for better layer caching
COPY ui-react/package.json ui-react/package-lock.json* ./
RUN npm ci

# Build React UI into /app/ui/static/app (configured in vite.config.ts)
COPY ui-react/ ./
RUN npm run build


FROM python:3.11-slim

# Build arguments for version info
ARG GIT_COMMIT=unknown
ARG GIT_BRANCH=main

# Set working directory
WORKDIR /app

# Install system dependencies including mosquitto, PostgreSQL, and Docker CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    nano \
    librsvg2-bin \
    mosquitto \
    postgresql \
    postgresql-contrib \
    curl \
    ca-certificates \
    gnupg \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

# Copy compiled React UI assets from build stage
COPY --from=ui-builder /app/ui/static/app /app/ui/static/app

# Copy bundled drivers and example pool configs
COPY bundled_config/ /app/bundled_config/

# Write version info to file (branch-commit format)
RUN echo "${GIT_BRANCH}-$(echo ${GIT_COMMIT} | cut -c1-7)" > /app/.git_commit

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create config directory
RUN mkdir -p /config

# Environment variables with defaults
ENV WEB_PORT=8080 \
    TZ=UTC \
    PUID=1000 \
    PGID=1000

# Expose web port
EXPOSE ${WEB_PORT}

# Use entrypoint script
CMD ["/entrypoint.sh"]
