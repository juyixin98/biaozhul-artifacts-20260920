# Build stage: sqlc-generated code is committed, so only the Go toolchain is
# needed.
FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/server ./cmd/server

FROM alpine:3.19
RUN apk add --no-cache ca-certificates wget && adduser -D -u 10001 app
COPY --from=build /out/server /app/server
COPY migrations /app/migrations
COPY seed /app/seed
USER app
WORKDIR /app
EXPOSE 8080
ENTRYPOINT ["/app/server"]
