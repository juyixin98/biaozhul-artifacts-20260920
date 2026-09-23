# syntax=docker/dockerfile:1
# Multi-stage build: static binaries, minimal runtime image.
FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/api  ./cmd/api \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/seed ./cmd/seed

FROM alpine:3.19
RUN adduser -D -u 10001 app && apk add --no-cache ca-certificates
COPY --from=build /out/api  /usr/local/bin/costlens-api
COPY --from=build /out/seed /usr/local/bin/costlens-seed
USER app
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/costlens-api"]
