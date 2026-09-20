# ---------- 构建 ----------
FROM golang:1.25-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /out/proofcycle ./cmd/server

# ---------- 运行 ----------
FROM debian:bookworm-slim
RUN useradd -r -u 10001 app \
    && mkdir -p /data/files \
    && chown -R app:app /data
USER app
COPY --from=build /out/proofcycle /usr/local/bin/proofcycle
ENV ADDR=:8080 \
    STORAGE_ROOT=/data/files \
    MAX_UPLOAD_MB=50
EXPOSE 8080
VOLUME ["/data/files"]
ENTRYPOINT ["proofcycle"]
