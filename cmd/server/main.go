// Command server runs the request-cancellation-propagation demo service.
//
// It starts two in-process fake downstream services and one application
// HTTP server. Everything is local; no external or production system is
// contacted.
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "application HTTP listen address")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if err := run(ctx, *addr); err != nil {
		log.Fatal(err)
	}
}
