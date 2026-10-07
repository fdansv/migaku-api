FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY migaku_api ./migaku_api
COPY LICENSE NOTICE ./
ENV MIGAKU_HOST=0.0.0.0 MIGAKU_PORT=8766 MIGAKU_RUNTIME_DIR=/data MIGAKU_EXTENSION_PATH=/extension MIGAKU_HEADLESS=1
EXPOSE 8766
CMD ["python", "-m", "migaku_api"]
