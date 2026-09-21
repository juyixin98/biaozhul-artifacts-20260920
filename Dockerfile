# Build stage
FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod go.sum* ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/forensiccore ./cmd/server

# Runtime stage
FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /app
COPY --from=build /out/forensiccore /app/forensiccore
COPY samples /data/evidence
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/app/forensiccore"]
