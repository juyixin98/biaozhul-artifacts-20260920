# syntax=docker/dockerfile:1
FROM golang:1.22-alpine AS build
WORKDIR /src
RUN apk add --no-cache git
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/vfxqueue ./cmd/vfxqueue

FROM alpine:3.20
RUN apk add --no-cache ca-certificates wget tzdata su-exec && \
    adduser -D -u 10001 vfx && \
    mkdir -p /data/assets /data/outputs && \
    chown -R 10001:10001 /data
WORKDIR /app
COPY --from=build /out/vfxqueue /app/vfxqueue
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
VOLUME ["/data"]
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["serve"]
