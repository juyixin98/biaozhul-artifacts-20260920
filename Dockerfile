# Build stage
FROM golang:1.22-bookworm AS build
WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/proofcycle ./cmd/server

# Runtime stage
FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /app
COPY --from=build /out/proofcycle /app/proofcycle

# Migrations are embedded in the binary; storage is mounted as a volume.
ENV HTTP_ADDR=":8080" \
    STORAGE_ROOT=/var/lib/proofcycle/files
VOLUME ["/var/lib/proofcycle/files"]
EXPOSE 8080

USER nonroot:nonroot
ENTRYPOINT ["/app/proofcycle"]
