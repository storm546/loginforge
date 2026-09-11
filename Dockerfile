FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc x11-utils xdotool matchbox-window-manager \
        novnc websockify \
        ca-certificates curl tini procps \
        fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        "playwright>=1.44" \
        "openai>=1.30" \
        "requests>=2.31" \
        "pyyaml>=6.0" \
        "pytest>=8.0"

# Chromium + its system deps (headed browser on Xvfb).
RUN playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY entrypoint.sh /app/entrypoint.sh
COPY agent /app/agent
COPY instructions /app/instructions
COPY targets /app/targets
RUN chmod +x /app/entrypoint.sh

VOLUME ["/profile", "/out"]
EXPOSE 6080

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
