# Build stage
FROM golang:1.25-bookworm AS build
WORKDIR /src

# Cache dependencies first.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
ARG BUILD_VERSION=dev
RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-s -w -X main.version=${BUILD_VERSION}" \
    -o /out/synapticgo ./cmd/server
RUN mkdir -p /out/data && chown -R 65532:65532 /out/data

# Runtime stage
FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /
COPY --from=build /out/synapticgo /synapticgo

# Pre-create the data directory owned by nonroot (uid 65532) so a fresh named
# volume inherits the ownership and the process can create its subdirectories.
COPY --from=build --chown=65532:65532 /out/data /data

# Migrations are embedded in the binary; the data dir holds the object store.
ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/synapticgo"]
