.PHONY: build test cover vet run seed demo fmt clean

BIN  := cpathtrace
ADDR := :8080
DATA := ./data

build:
	go build -o $(BIN) ./cmd/cpathtrace

test:
	go test ./...

cover:
	go test -coverprofile=/tmp/cpathtrace.cover ./...
	go tool cover -func=/tmp/cpathtrace.cover

vet:
	go vet ./...

fmt:
	gofmt -l -w .

run: build
	./$(BIN) serve --addr $(ADDR) --data $(DATA)

seed: build
	./$(BIN) seed --data $(DATA)

demo: build
	@echo "Run './$(BIN) serve' in one terminal, then ./examples/curl-demo.sh"

clean:
	rm -f $(BIN)
