FROM python:3.12-slim

ARG PUID=1000
ARG PGID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd -g "${PGID}" app \
    && useradd -u "${PUID}" -g app -m -s /usr/sbin/nologin app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install . \
    && rm -rf /app/src

COPY deploy/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

RUN mkdir -p /music /data /config \
    && chown -R app:app /data /config

USER app

VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["daemon"]