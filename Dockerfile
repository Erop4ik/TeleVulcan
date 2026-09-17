FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN groupadd -r -g 10001 bot && useradd -r -u 10001 -g bot -d /app -s /usr/sbin/nologin bot \
    && mkdir -p /app /data && chown bot:bot /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=bot:bot vulcanbot ./vulcanbot

USER bot
ENV DB_PATH=/data/vulcanbot.sqlite3 WEB_PORT=8765
EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ.get('WEB_PORT','8765'),timeout=4)" || exit 1

CMD ["python", "-m", "vulcanbot"]
