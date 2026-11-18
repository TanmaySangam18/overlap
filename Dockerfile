# OVERLAP — production image.
#
# The SQLite database must live on a persistent volume mounted at /data
# (Render persistent disk, Fly volume, or a host directory). Without one,
# every redeploy wipes the data. See DEPLOY-PROD.md.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

RUN mkdir -p /data \
    && useradd --create-home overlap \
    && chown -R overlap:overlap /data /app
USER overlap

ENV OVERLAP_DB_PATH=/data/overlap.db \
    OVERLAP_ENV=prod \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# $PORT is set by Render/Fly; default to 8000 for a plain `docker run`.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
