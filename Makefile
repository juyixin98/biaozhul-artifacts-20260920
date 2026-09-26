.PHONY: all build test race cover report demo clean

build:
	go build ./...

test:
	go test -count=1 -timeout 120s ./...

race:
	go test -race -count=1 -timeout 240s ./...

cover:
	go test -race -count=1 -coverprofile=results/coverage.out -timeout 240s ./...
	go tool cover -func=results/coverage.out | tail -1

report:
	go test -json -race -count=1 -timeout 240s ./... > results/raw-test-events.jsonl
	go run ./cmd/testreport -in results/raw-test-events.jsonl -out results/test-report.json

vet:
	go vet ./...

fmt:
	gofmt -w .

clean:
	rm -rf bin
