# Build the manager binary (offline-friendly: uses the vendored dependency tree).
FROM golang:1.22 AS builder
WORKDIR /workspace
COPY go.mod go.sum ./
COPY vendor/ vendor/
COPY api/ api/
COPY cmd/ cmd/
COPY internal/ internal/
RUN CGO_ENABLED=0 GOOS=linux GOFLAGS=-mod=vendor go build -trimpath -ldflags="-s -w" -o manager ./cmd/manager

FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /
COPY --from=builder /workspace/manager /manager
USER 65532:65532
ENTRYPOINT ["/manager"]
