# The manager image. Dependencies are vendored (vendor/) so the image build
# needs no network access (-mod=vendor + GOPROXY=off). The worker Job does NOT
# use this image: it runs the embedded shell script inside the kind node image
# (which already contains bash/coreutils/kubectl).
FROM golang:1.22 AS build
WORKDIR /src
COPY . .
ARG TARGETOS=linux
ARG TARGETARCH=amd64
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} GOPROXY=off \
    go build -mod=vendor -trimpath -ldflags="-s -w" -o /out/manager ./cmd/manager

# The binary is statically linked (CGO disabled). The manager verifies the
# apiserver with the ServiceAccount CA mounted from the pod, so no system CA
# package or build-time network is required in the runtime image.
FROM debian:bookworm-slim
COPY --from=build /out/manager /usr/local/bin/manager
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/manager"]
