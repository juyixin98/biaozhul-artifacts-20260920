// Command client is the fault-injection test client. It runs one
// streaming request in a selectable mode (fast, slow, disconnect) and
// prints the structured result as JSON on stdout.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"streamback/internal/client"
)

func main() {
	url := flag.String("url", "http://127.0.0.1:8080/stream?count=10&itemBytes=1024", "full /stream URL with query parameters")
	mode := flag.String("mode", string(client.ModeFast), "fast | slow | disconnect")
	readDelay := flag.Duration("read-delay", 100*time.Millisecond, "per-record delay in slow mode")
	disconnectAfter := flag.Int("disconnect-after", 3, "records to read before closing the connection in disconnect mode")
	maxRecords := flag.Int("max-records", 100000, "safety cap on records read")
	timeout := flag.Duration("timeout", 60*time.Second, "overall run timeout")
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()

	res := client.Run(ctx, client.Options{
		URL:             *url,
		Mode:            client.Mode(*mode),
		ReadDelay:       *readDelay,
		DisconnectAfter: *disconnectAfter,
		MaxRecords:      *maxRecords,
	})

	out, err := json.MarshalIndent(res, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "marshal result: %v\n", err)
		os.Exit(1)
	}
	fmt.Println(string(out))

	// Exit code reflects whether the run behaved as its mode intends.
	switch client.Mode(*mode) {
	case client.ModeDisconnect:
		if !res.Disconnected {
			os.Exit(1)
		}
	default:
		if res.Failure != "" {
			os.Exit(1)
		}
	}
}
