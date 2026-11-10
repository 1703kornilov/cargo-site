FROM python:3.12-slim
WORKDIR /app
COPY server.py .
COPY static ./static
ENV DB_PATH=/data/app.db TRUST_PROXY=1 HOST=0.0.0.0 PORT=8000 PYTHONUNBUFFERED=1
RUN useradd -m app && mkdir /data && chown app /data
USER app
EXPOSE 8000
CMD ["python", "server.py"]
