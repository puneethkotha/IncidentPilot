FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir \
    "fastapi>=0.110" "uvicorn>=0.29" "prometheus-client>=0.20" "pydantic>=2"

COPY app.py /app/app.py

ENV SERVICE_NAME=payment-service POOL_SIZE=10
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
