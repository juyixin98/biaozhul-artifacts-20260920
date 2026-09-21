FROM golang:1.22-bookworm AS build
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /bin/sircc-server ./cmd/server

FROM debian:bookworm-slim
RUN useradd --system --no-create-home sircc
COPY --from=build /bin/sircc-server /usr/local/bin/sircc-server
USER sircc
EXPOSE 8080
ENV ADDR=:8080
ENTRYPOINT ["sircc-server"]
