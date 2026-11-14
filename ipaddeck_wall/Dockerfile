FROM python:3.12-alpine
COPY server.py wall.default.json /app/
COPY web /app/web
ENTRYPOINT ["python3", "-u", "/app/server.py"]
