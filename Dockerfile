# Build a fully static binary (no libc, embedded tzdata).
FROM golang:1.25-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/signalboard ./cmd/signalboard

FROM scratch
COPY --from=build /out/signalboard /signalboard
EXPOSE 8080
ENTRYPOINT ["/signalboard"]
