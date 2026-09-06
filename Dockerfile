FROM python:3.11-slim

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .
COPY VERSION ./VERSION

# Build identity, surfaced by /api/health. APP_VERSION defaults to empty so a
# local `docker build` falls back to the VERSION file rather than reporting a
# fake version; BUILD_REF/BUILD_SHA staying "unknown" is what marks it as a
# non-CI build.
ARG APP_VERSION=
ARG BUILD_SHA=unknown
ARG BUILD_REF=unknown
ENV APP_VERSION=${APP_VERSION} \
    BUILD_SHA=${BUILD_SHA} \
    BUILD_REF=${BUILD_REF}

RUN mkdir -p /config

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "60", "app:app"]
