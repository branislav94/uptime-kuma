FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "uptime-kuma-api2>=2.9" "requests>=2.31" "tldextract>=5.1"
COPY kuma_sync.py .
CMD ["python", "-u", "kuma_sync.py", "--loop"]
