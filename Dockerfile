# kaal-server in a container.
#
# Two volumes: /srv/kaal holds the vaults, accounts and backups; /etc/kaal holds
# the key, apart from the data so that a copy of the data volume is unreadable
# by itself. deploy/docker/install.sh sets all of this up; see docs/install.md.
FROM python:3.14-slim

ARG KAAL_EXTRAS=server,ai
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 999 --home-dir /srv/kaal --shell /usr/sbin/nologin kaal \
 && install -d -o kaal -g kaal -m 700 /srv/kaal /etc/kaal

WORKDIR /opt/kaal
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY kaal ./kaal
RUN pip install --no-cache-dir ".[${KAAL_EXTRAS}]"
COPY deploy/docker/entrypoint.sh /usr/local/bin/kaal-entrypoint

ENV KAAL_HOME=/srv/kaal \
    KAAL_VAULT=/srv/kaal/data \
    KAAL_TMP_DIR=/srv/kaal/tmp \
    KAAL_BACKUP_DIR=/srv/kaal/backups \
    TMPDIR=/srv/kaal/tmp \
    KAAL_KEY_PROVIDER=file \
    KAAL_KEY_FILE=/etc/kaal/master.key \
    KAAL_CONFIG=/nonexistent \
    KAAL_LOCAL=1 \
    KAAL_BIND=0.0.0.0 \
    KAAL_ALLOW_NON_LOOPBACK=1 \
    PYTHONDONTWRITEBYTECODE=1

USER kaal
VOLUME ["/srv/kaal", "/etc/kaal"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/v1/health', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["kaal-entrypoint"]
CMD ["kaal-server", "serve"]
