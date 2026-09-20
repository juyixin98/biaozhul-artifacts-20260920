FROM golang:1.25-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /bin/desklens ./cmd/server

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /bin/desklens /desklens
COPY seed/ /seed/
ENV ADDR=:8080 SEED_FILE=/seed/seed.sql
EXPOSE 8080
ENTRYPOINT ["/desklens"]
