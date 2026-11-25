FROM python:3.12-slim
WORKDIR /app
COPY server.py VERSION ./
COPY shopdesk ./shopdesk
COPY static ./static
ENV SHOPDESK_DATA_DIR=/data SHOPDESK_PORT=8080 SHOPDESK_HOST=0.0.0.0
VOLUME /data
EXPOSE 8080
CMD ["python", "server.py"]
