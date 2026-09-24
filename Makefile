.PHONY: build test test-all run infra up down seed sample restart bad retained faults-on faults-off dropack state metrics logs acceptance clean

MQTT_PORT ?= 11883
PG_PORT   ?= 55433
HTTP_PORT ?= 8079
MQTT      ?= 127.0.0.1:$(MQTT_PORT)
PG_DSN    ?= postgres://mqttredel:mqttredel@127.0.0.1:$(PG_PORT)/mqttredel?sslmode=disable

build:
	go build -o bin/ ./cmd/...

test:
	go test ./...

# 需要 `make up` 已启动依赖：额外运行 PostgreSQL 集成测试。
test-all:
	PG_DSN="$(PG_DSN)" go test -count=1 ./...

up:
	docker compose up -d
	@for i in $$(seq 1 30); do docker exec mqttredel-pg pg_isready -U mqttredel >/dev/null 2>&1 && break; sleep 1; done
	@echo "mosquitto 127.0.0.1:$(MQTT_PORT) | postgres 127.0.0.1:$(PG_PORT)"

down:
	docker compose down

infra: up

run: build
	PG_DSN="$(PG_DSN)" MQTT_ADDR="$(MQTT)" HTTP_PORT="$(HTTP_PORT)" ./bin/consumer

seed: build
	./bin/seed -pg "$(PG_DSN)" -id dev-001
	./bin/seed -pg "$(PG_DSN)" -id dev-002

sample:
	./bin/device -mqtt "$(MQTT)" -id dev-001 -boot 1 -count 5

restart:
	./bin/device -mqtt "$(MQTT)" -id dev-001 -boot 2 -count 3

bad:
	./bin/inject -mqtt "$(MQTT)" badjson dev-002
	./bin/inject -mqtt "$(MQTT)" badsig  dev-002

retained:
	./bin/inject -mqtt "$(MQTT)" retained dev-001

faults-on:
	curl -s -X POST "http://127.0.0.1:$(HTTP_PORT)/faults/arm?device=dev-001"; echo

faults-off:
	curl -s -X POST "http://127.0.0.1:$(HTTP_PORT)/faults/clear"; echo

dropack:
	curl -s -X POST "http://127.0.0.1:$(HTTP_PORT)/faults/dropack?device=dev-001"; echo

state:
	curl -s http://127.0.0.1:$(HTTP_PORT)/state | python3 -m json.tool

metrics:
	curl -s http://127.0.0.1:$(HTTP_PORT)/metrics | python3 -m json.tool

logs:
	docker compose logs --tail=50 mosquitto

acceptance:
	bash scripts/acceptance.sh

clean:
	docker compose down -v
	rm -rf bin
