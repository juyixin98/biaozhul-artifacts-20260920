# syntax=docker/dockerfile:1

FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod go.sum* ./
RUN go mod download
COPY . .
# timetzdata 内置时区库，运行镜像不依赖系统 tzdata
RUN CGO_ENABLED=0 GOOS=linux go build -tags timetzdata -trimpath -ldflags="-s -w" -o /out/api ./cmd/server
RUN CGO_ENABLED=0 GOOS=linux go build -tags timetzdata -trimpath -ldflags="-s -w" -o /out/seed-events ./cmd/seed-events

FROM debian:12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /out/api /app/api
COPY --from=build /out/seed-events /app/seed-events
COPY migrations /app/migrations
COPY samples /app/samples
EXPOSE 8080
USER nobody
ENTRYPOINT ["/app/api"]
