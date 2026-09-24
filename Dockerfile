# Multi-stage build; the embedded tzdata ships inside the binary, so the
# runtime image needs no zoneinfo package or network access.
FROM golang:1.23-bookworm AS build
WORKDIR /src
COPY . .
RUN GOTOOLCHAIN=local CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" \
    -o /out/tztrig ./cmd/tztrig

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/tztrig /tztrig
USER nonroot
EXPOSE 8080
ENTRYPOINT ["/tztrig"]
CMD ["-addr=:8080", "-state=/tmp/tztrig/state.json"]
