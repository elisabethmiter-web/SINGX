FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATA_DIR=/data PORT=8000
VOLUME ["/data"]
EXPOSE 8000
CMD ["sh", "-c", "gunicorn -w 2 --threads 4 --timeout 120 -b 0.0.0.0:${PORT} app:app"]
