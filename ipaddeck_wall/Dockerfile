ARG BUILD_FROM
FROM $BUILD_FROM
RUN apk add --no-cache python3
COPY server.py wall.default.json /app/
COPY web /app/web
ENTRYPOINT ["python3", "-u", "/app/server.py"]
