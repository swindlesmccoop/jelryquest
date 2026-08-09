FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn httpx

COPY addon_bridge/main.py main.py

EXPOSE 8090

CMD ["python", "main.py"]
