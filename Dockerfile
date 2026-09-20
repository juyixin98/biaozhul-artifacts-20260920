FROM golang:1.25 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /bin/sircc-server ./cmd/server

FROM gcr.io/distroless/static-debian12
COPY --from=build /bin/sircc-server /sircc-server
EXPOSE 8080
ENTRYPOINT ["/sircc-server"]
