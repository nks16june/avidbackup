# AvidBackup — local-first website + database backup utility
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      mariadb-client \
      postgresql-client \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin avidbackup

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY examples ./examples
RUN pip install --no-cache-dir . \
    && mkdir -p /data \
    && chown -R avidbackup:avidbackup /data /app

ENV AVIDBACKUP_HOME=/data \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8787

USER avidbackup

# Bind 0.0.0.0 only when AVIDBACKUP_UI_TOKEN is set (enforced by entrypoint + app).
COPY --chown=avidbackup:avidbackup docker/entrypoint.sh /app/entrypoint.sh
# Windows checkouts may lack +x; entrypoint invoked via sh
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["app", "--host", "0.0.0.0", "--port", "8787", "--no-tray"]
