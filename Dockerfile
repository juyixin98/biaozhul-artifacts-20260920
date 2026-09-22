# Build stage
FROM golang:1.22-alpine AS build
WORKDIR /src
RUN apk add --no-cache ca-certificates git
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath \
    -ldflags="-s -w" -o /out/costlens-server ./cmd/server

# Runtime stage
FROM alpine:3.20
RUN apk add --no-cache ca-certificates tzdata && \
    adduser -D -u 10001 costlens
USER costlens
WORKDIR /app
COPY --from=build /out/costlens-server /app/costlens-server
EXPOSE 8080
ENTRYPOINT ["/app/costlens-server"]
