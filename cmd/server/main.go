// Command server runs the immutable-artifact range service on loopback.
// It seeds a small set of in-memory artifacts; nothing is read from disk and
// no external system is contacted.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"httprange/internal/artifact"
	"httprange/internal/clock"
	"httprange/internal/origin"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	gzip := flag.Bool("gzip", false, "offer gzip selected representation")
	maxRanges := flag.Int("max-ranges", 5, "maximum accepted Range members")
	flag.Parse()

	clk := clock.System{}
	store := seedStore(clk.Now())

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	h, err := origin.Start(ctx, origin.Config{
		Addr:       *addr,
		Store:      store,
		Clock:      clk,
		EnableGzip: *gzip,
		MaxRanges:  *maxRanges,
	})
	if err != nil {
		log.Fatalf("server: %v", err)
	}
	fmt.Printf("range service listening on %s\n", h.BaseURL)
	fmt.Println("artifacts: GET /  | object: GET /artifacts/<id>")

	<-ctx.Done()
	fmt.Println("\nshutting down")
	if err := h.Shutdown(); err != nil {
		log.Printf("shutdown: %v", err)
	}
	os.Exit(0)
}

func seedStore(now time.Time) *artifact.Store {
	makeBytes := func(n int, seed byte) []byte {
		b := make([]byte, n)
		for i := range b {
			b[i] = seed + byte(i%7)
		}
		return b
	}
	store := artifact.NewStore()
	store.Put(artifact.New("tiny.bin", makeBytes(26, 0), "application/octet-stream", now))
	store.Put(artifact.New("sample.bin", makeBytes(1024, 64), "application/octet-stream", now))
	store.Put(artifact.New("hello.txt", []byte("hello range world\n"), "text/plain; charset=utf-8", now))
	store.Put(artifact.New("empty.bin", []byte{}, "application/octet-stream", now))
	return store
}
