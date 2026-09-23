.PHONY: build test demo vet clean

BIN := bin/vcconflict

build:
	go build -o $(BIN) ./cmd/vcconflict

test:
	go test -race -count=1 ./...

vet:
	go vet ./...

demo: build
	@for f in examples/0*.json; do \
		echo "=== $$f ==="; \
		$(BIN) -f $$f | python3 -c 'import json,sys; r=json.load(sys.stdin); print("stats:", r["stats"])'; \
	done

clean:
	rm -rf bin
