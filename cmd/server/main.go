// Command server runs the chunked streaming backend with the in-process
// fake upstream. It serves until SIGINT/SIGTERM, then shuts down gracefully.
package main

import (
	"context"
	"flag"
	"log"
	"os/signal"
	"syscall"
	"time"

	"streambp/internal/clock"
	"streambp/internal/server"
	"streambp/internal/upstream"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	buffer := flag.Int("buffer", 8, "per-connection bounded buffer capacity (items)")
	maxItem := flag.Int("max-item-bytes", 1<<20, "max bytes per single item")
	maxBuffered := flag.Int64("max-buffered-bytes", 16<<20, "max total buffered bytes across connections")
	items := flag.Int("items", 100, "default items per stream")
	itemSize := flag.Int("item-size", 256, "default item payload bytes")
	itemDelayMs := flag.Int("item-delay-ms", 10, "default per-item upstream delay")
	flag.Parse()

	cfg := server.Config{
		BufferCapacity:   *buffer,
		MaxItemBytes:     *maxItem,
		MaxBufferedBytes: *maxBuffered,
		Defaults: upstream.Config{
			ItemCount: *items,
			ItemSize:  *itemSize,
			Delay:     time.Duration(*itemDelayMs) * time.Millisecond,
			FailAfter: -1,
		},
	}
	srv, err := server.New(cfg, clock.Real{})
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	log.Printf("listening on http://%s/stream (buffer=%d max_item=%d max_buffered=%d)",
		*addr, *buffer, *maxItem, *maxBuffered)
	if err := srv.ListenAndServe(ctx, *addr); err != nil {
		log.Fatalf("serve: %v", err)
	}
	log.Printf("shutdown complete")
}
