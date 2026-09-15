FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
WORKDIR /app
COPY requirements.txt requirements-runtime.txt ./
RUN pip install -r requirements-runtime.txt \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright \
    && groupadd -g 1000 app && useradd -m -u 1000 -g app app
COPY --chown=app:app . .
RUN mkdir -p outputs/runs outputs/dashboard_jobs memory && chown -R app:app outputs memory
USER app
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/presentation',timeout=3)"
CMD ["python", "-u", "utils/dashboard_server.py", "--host", "0.0.0.0", "--port", "8765"]
