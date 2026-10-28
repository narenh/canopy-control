FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py canopy_account.py live_push.py isbtv.py lounge.py oembed.py shorts.py ./
# Not root. uid/gid 10001 is shared with the isponsorblocktv service (docker-compose.yml), which
# reads the config.json this one writes into /isbtv. A named volume mounted on /data or /isbtv
# starts out owned by this user, since the directories are.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && mkdir -p /data /isbtv && chown 10001:10001 /data /isbtv
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
