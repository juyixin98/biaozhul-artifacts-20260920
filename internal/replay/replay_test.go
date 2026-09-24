package replay

import (
	"context"
	"io"
	"log"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"modbus-receipt/internal/server"
	"modbus-receipt/internal/storage"
)

func startTestServer(t *testing.T, bank int) string {
	t.Helper()
	dir := t.TempDir()
	store, err := storage.NewStore(context.Background(), storage.Options{
		DSN:          "file:" + filepath.Join(dir, "r.db") + "?_txlock=immediate",
		BankSize:     bank,
		AllowedUnits: []byte{1},
	})
	if err != nil {
		t.Fatal(err)
	}
	srv := server.New(server.Config{ListenAddr: "127.0.0.1:0", Store: store},
		log.New(io.Discard, "", 0))
	go func() { _ = srv.Serve(context.Background()) }()
	for srv.Addr() == nil {
		time.Sleep(time.Millisecond)
	}
	t.Cleanup(func() { srv.Shutdown(); store.Close() })
	return srv.Addr().String()
}

// Runs the shipped examples/demo.jsonl end to end; every step must pass.
func TestDemoScenario(t *testing.T) {
	addr := startTestServer(t, 32)
	steps, err := ParseFile(filepath.Join("..", "..", "examples", "demo.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	r := NewRunner(addr, 3*time.Second)
	defer r.Close()
	results := r.Run(steps)

	failed := 0
	for _, res := range results {
		if !res.OK {
			t.Errorf("line %d (%s): %s", res.LineNo, res.Op, res.Error)
			failed++
		}
	}
	if failed > 0 {
		t.Fatalf("%d scenario step(s) failed", failed)
	}
}

func TestParseRejectsBadHex(t *testing.T) {
	if _, err := Parse(strings.NewReader(`{"op":"raw","bytes":"zz"}`)); err == nil {
		t.Fatal("expected hex parse error")
	}
	if _, err := Parse(strings.NewReader(`{"op":"read","start":0,"qty":0}`)); err == nil {
		t.Fatal("expected qty validation error")
	}
	if _, err := Parse(strings.NewReader(`{"op":"bogus"}`)); err == nil {
		t.Fatal("expected unknown op error")
	}
}

func TestExpectErrorAssertion(t *testing.T) {
	addr := startTestServer(t, 4)
	steps, err := Parse(strings.NewReader(
		`{"op":"read","unit":1,"start":0,"qty":99,"expect_error":"0x02"}` + "\n" +
			`{"op":"read","unit":1,"start":0,"qty":1,"expect_error":"0x02"}` + "\n"))
	if err != nil {
		t.Fatal(err)
	}
	r := NewRunner(addr, time.Second)
	defer r.Close()
	res := r.Run(steps)
	if !res[0].OK {
		t.Fatalf("expected-error step should pass: %+v", res[0])
	}
	if res[1].OK {
		t.Fatalf("unmet expect_error must fail: %+v", res[1])
	}
}
