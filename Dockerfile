FROM python:3.14-slim AS build
WORKDIR /src
COPY . .
RUN python3 build_zipapp.py

FROM python:3.14-slim
LABEL org.opencontainers.image.source="https://github.com/aivarsk/otelite" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.title="otelite" \
      org.opencontainers.image.description="The disposable garage edition of OTEL"
COPY --from=build /src/dist/otelite-*.pyz /opt/otelite.pyz
ENV OTELITE_DATA_DIR=/data
VOLUME /data
EXPOSE 4318
ENTRYPOINT ["python3", "/opt/otelite.pyz"]
