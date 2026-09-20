# 多阶段构建：编译静态二进制（CGO 关闭，sqlite 驱动为纯 Go）。
FROM golang:1.25-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/forensiccore ./cmd/server

# 运行镜像：非 root 用户，证据目录只读挂载由 compose 控制。
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r forensic && useradd -r -g forensic -d /home/forensic -m forensic
USER forensic
WORKDIR /app
COPY --from=build /out/forensiccore /app/forensiccore
EXPOSE 8080
ENTRYPOINT ["/app/forensiccore"]
