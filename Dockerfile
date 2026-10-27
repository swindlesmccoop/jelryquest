FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

ENV PYTHONUNBUFFERED=1
ENV WEB_PORT=8080
EXPOSE 8080

CMD ["python", "-m", "backend.main"]
