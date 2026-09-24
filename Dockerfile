# Builds both the decision server and the metrics stub into one image; the
# compose file selects the binary via `command`. Dependencies are vendored
# (./vendor) so the build needs no network access to a Go module proxy.
FROM golang:1.22-alpine AS build
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -mod=vendor -trimpath -o /out/server ./cmd/server \
 && CGO_ENABLED=0 GOOS=linux go build -mod=vendor -trimpath -o /out/stub  ./cmd/stub

# Minimal runtime: static binaries, no package downloads, no shell.
FROM scratch
COPY --from=build /out/server /app/server
COPY --from=build /out/stub  /app/stub
EXPOSE 8080 8081
# No ENTRYPOINT: compose selects /app/server or /app/stub explicitly.
