FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY blast_radius ./blast_radius
RUN pip install . && rm -rf /src \
    && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin blastradius

USER 10001
WORKDIR /data
EXPOSE 8098
ENV BLAST_RADIUS_CONFIG=/config/config.yaml

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8098/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "blast_radius.app"]
