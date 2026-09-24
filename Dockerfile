# Multi-stage build produces a small distroless image.
FROM golang:1.22 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
ARG TARGETOS=linux
ARG TARGETARCH=amd64
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags="-s -w" -o /manager ./cmd/manager

FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /
COPY --from=build /manager /manager
USER nonroot:nonroot
ENTRYPOINT ["/manager"]
