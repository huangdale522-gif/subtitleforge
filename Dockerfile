FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

RUN useradd --create-home --uid 1000 subtitleforge \
    && mkdir -p /data /media \
    && chown -R subtitleforge:subtitleforge /data /media
USER subtitleforge

EXPOSE 8600
CMD ["uvicorn", "subtitleforge.api:app", "--host", "0.0.0.0", "--port", "8600"]

