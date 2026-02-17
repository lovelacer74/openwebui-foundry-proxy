FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app.py .
COPY config.yaml .

# Non-root user
RUN useradd -m proxyuser
USER proxyuser

EXPOSE 8000

# Uvicorn with sensible defaults
# - 2 workers handles concurrency without over-provisioning on ACA
# - timeout-keep-alive prevents connection drops on slow models
CMD ["uvicorn", "app:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--timeout-keep-alive", "120", \
     "--log-level", "info"]
