// Command client is the fault-injection streaming client. It prints one
// structured JSON result line to stdout and exits non-zero if the received
// sequence was inconsistent or the run failed unexpectedly.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"streambp/internal/client"
)

func main() {
	url := flag.String("url", "http://127.0.0.1:8080/stream", "stream URL")
	mode := flag.String("mode", "fast", "fast | slow")
	readDelayMs := flag.Int("read-delay-ms", 0, "delay before each record read (slow mode default: 25)")
	disconnectAfter := flag.Int("disconnect-after", 0, "close connection after N records (0 = never)")
	timeoutMs := flag.Int("timeout-ms", 30000, "overall run timeout")
	flag.Parse()

	delay := time.Duration(*readDelayMs) * time.Millisecond
	if *mode == "slow" && delay == 0 {
		delay = 25 * time.Millisecond
	}
	res := client.Run(context.Background(), client.Options{
		URL:             *url,
		ReadDelay:       delay,
		DisconnectAfter: *disconnectAfter,
		Timeout:         time.Duration(*timeoutMs) * time.Millisecond,
	})
	out, _ := json.Marshal(res)
	fmt.Println(string(out))
	if res.Failure != "" || !res.SequenceOK {
		os.Exit(1)
	}
}
