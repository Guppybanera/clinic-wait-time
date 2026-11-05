FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home clinic && chown -R clinic /srv
USER clinic

# Production defaults; set SECRET_KEY, DATA_KEY, DATABASE_URL, BASE_URL and Twilio vars on the host.
ENV DEV_MODE=false HTTPS_ONLY_COOKIES=true PORT=8000
EXPOSE 8000
# One worker: the background job runs inside the app and must not run twice.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
