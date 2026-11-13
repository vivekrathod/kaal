# kaal-server in a container. The vault and token file live in the /srv/kaal
# volume; the key is mounted as a file readable only by uid 999 (see README).
FROM python:3.14-slim

ARG KAAL_EXTRAS=server
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 999 --home-dir /srv/kaal --shell /usr/sbin/nologin kaal

WORKDIR /opt/kaal
COPY pyproject.toml README.md ./
COPY kaal ./kaal
RUN pip install --no-cache-dir ".[${KAAL_EXTRAS}]"

ENV KAAL_HOME=/srv/kaal \
    KAAL_VAULT=/srv/kaal/data \
    KAAL_TMP_DIR=/srv/kaal/tmp \
    TMPDIR=/srv/kaal/tmp \
    KAAL_KEY_PROVIDER=file \
    KAAL_KEY_FILE=/run/secrets/kaal_master_key \
    KAAL_BIND=0.0.0.0 \
    KAAL_ALLOW_NON_LOOPBACK=1

USER kaal
VOLUME ["/srv/kaal"]
EXPOSE 8765
CMD ["kaal-server", "serve"]
