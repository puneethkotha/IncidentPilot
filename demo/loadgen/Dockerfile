FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "httpx>=0.27"
COPY loadgen.py /app/loadgen.py

ENV TARGET_URL=http://payment-service:8080 CONCURRENCY=15 USERS=40
CMD ["python", "loadgen.py"]
