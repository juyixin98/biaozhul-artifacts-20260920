# Static, distroless image. The Linux binary is built ON THE HOST (modules
# are already in the local Go cache) so `docker build` needs no network:
#
#   GOOS=linux GOARCH=amd64 CGO_ENABLED=0 \
#       go build -trimpath -ldflags '-s -w' -o bin/manager ./cmd/manager
#   docker build -t quota-controller:dev .
FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /
COPY bin/manager /manager
USER 65532:65532
ENTRYPOINT ["/manager"]
