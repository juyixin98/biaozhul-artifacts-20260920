// Command sse-client is a demo SSE consumer that exercises resume semantics.
//
// On disconnect it reconnects automatically with Last-Event-ID set to the
// highest event ID it has seen, dedupes events by ID, and honours the server's
// "reset" control event by discarding local state and resuming from the
// server-provided oldest ID.
//
// Usage:
//
//	sse-client stream  [-addr http://127.0.0.1:8080] [-duration 30s] [-reset-every N]
//	sse-client publish [-addr ...] "event payload text"
//
// -reset-every N closes and reconnects the connection after every N events
// received (used to demonstrate replay across the replay/live seam).
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"sseserver/internal/sse"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "stream":
		streamCmd(os.Args[2:])
	case "publish":
		publishCmd(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: sse-client stream [-addr URL] [-duration D] [-reset-every N]")
	fmt.Fprintln(os.Stderr, "       sse-client publish [-addr URL] <data>")
	os.Exit(2)
}

func streamCmd(args []string) {
	fs := flag.NewFlagSet("stream", flag.ExitOnError)
	addr := fs.String("addr", "http://127.0.0.1:8080", "server base URL")
	duration := fs.Duration("duration", 30*time.Second, "total run time")
	resetEvery := fs.Int("reset-every", 0, "force disconnect+reconnect after every N events (0 = never)")
	verbose := fs.Bool("v", false, "log control events and reconnects")
	backoffFlag := fs.Duration("backoff", 200*time.Millisecond, "fixed reconnect backoff (exponential growth is disabled when set >0)")
	_ = fs.Parse(args)

	logger := log.New(os.Stderr, "sse-client ", log.LstdFlags|log.Lmicroseconds)

	ctx, cancel := context.WithTimeout(context.Background(), *duration)
	defer cancel()

	// lastID is the resume cursor; seen is the dedupe set.
	var lastID uint64
	seen := make(map[uint64]bool)
	var ordered []uint64
	var dupCount int
	var gaps []string

	backoff := *backoffFlag
	if backoff <= 0 {
		backoff = 200 * time.Millisecond
	}
	conn := 0

	for ctx.Err() == nil {
		conn++
		received, err := runOneConnection(ctx, *addr, lastID, func(ev sse.Event, reset *resetPayload, errEvent *errPayload) error {
			if errEvent != nil {
				logger.Printf("server error frame: %s; will reconnect", errEvent.Reason)
				return errForceReconnect
			}
			if reset != nil {
				logger.Printf("RESET reason=%s oldest=%d last=%d — discarding %d local ids, resume at oldest-1=%d",
					reset.Reason, reset.Oldest, reset.Last, len(seen), reset.Oldest-1)
				lastID = reset.Oldest - 1
				seen = make(map[uint64]bool)
				ordered = nil
				return errForceReconnect
			}
			id, perr := strconv.ParseUint(ev.ID, 10, 64)
			if perr != nil {
				logger.Printf("event with non-numeric id %q ignored", ev.ID)
				return nil
			}
			if seen[id] {
				dupCount++
				if *verbose {
					logger.Printf("duplicate id=%d ignored (total dups=%d)", id, dupCount)
				}
				return nil
			}
			if len(ordered) > 0 && id != ordered[len(ordered)-1]+1 {
				gaps = append(gaps, fmt.Sprintf("out-of-order id: got %d after %d", id, ordered[len(ordered)-1]))
			}
			seen[id] = true
			ordered = append(ordered, id)
			if id > lastID {
				lastID = id
			}
			fmt.Printf("event id=%d data=%q\n", id, ev.Data)
			if *resetEvery > 0 && len(ordered)%*resetEvery == 0 {
				return errForceReconnect
			}
			return nil
		})
		if err != nil && !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded) && ctx.Err() == nil {
			if *verbose {
				logger.Printf("connection %d ended: %v (received this conn=%d)", conn, err, received)
			}
		}
		if ctx.Err() != nil {
			break
		}
		if *verbose || errors.Is(err, errForceReconnect) {
			logger.Printf("reconnecting with Last-Event-ID=%d (backoff=%s)", lastID, backoff)
		}
		select {
		case <-ctx.Done():
		case <-time.After(backoff):
		}
		if *backoffFlag <= 0 && backoff < 2*time.Second {
			backoff *= 2
		}
		if *backoffFlag > 0 {
			backoff = *backoffFlag
		}
	}

	// Final accounting: verify contiguous delivery across the observed window.
	var missing []uint64
	if len(ordered) > 0 {
		for id := ordered[0]; id <= ordered[len(ordered)-1]; id++ {
			if !seen[id] {
				missing = append(missing, id)
			}
		}
	}
	fmt.Fprintf(os.Stderr, "summary: connections=%d unique_events=%d range=[%d,%d] duplicates_dropped=%d missing=%v gap_alerts=%d\n",
		conn, len(ordered), firstOrZero(ordered), lastOrZero(ordered), dupCount, missing, len(gaps))
	if len(missing) > 0 || len(gaps) > 0 {
		os.Exit(1)
	}
}

func firstOrZero(xs []uint64) uint64 {
	if len(xs) == 0 {
		return 0
	}
	return xs[0]
}

func lastOrZero(xs []uint64) uint64 {
	if len(xs) == 0 {
		return 0
	}
	return xs[len(xs)-1]
}

var errForceReconnect = errors.New("forced reconnect")

type resetPayload struct {
	Reason string `json:"reason"`
	Oldest uint64 `json:"oldest"`
	Last   uint64 `json:"last"`
	Note   string `json:"note"`
}

type errPayload struct {
	Reason string `json:"reason"`
}

type handleFunc func(ev sse.Event, reset *resetPayload, errEvent *errPayload) error

func runOneConnection(ctx context.Context, base string, lastID uint64, handle handleFunc) (int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/events", nil)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Accept", "text/event-stream")
	if lastID > 0 {
		req.Header.Set("Last-Event-ID", strconv.FormatUint(lastID, 10))
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return 0, fmt.Errorf("unexpected status %d: %s", resp.StatusCode, bytes.TrimSpace(body))
	}

	dec := sse.NewDecoder(resp.Body)
	received := 0
	for {
		ev, err := dec.Next()
		if err != nil {
			return received, err // io.EOF or network error -> caller reconnects
		}
		received++
		switch ev.Type {
		case "reset":
			var rp resetPayload
			if jerr := json.Unmarshal([]byte(ev.Data), &rp); jerr != nil {
				return received, fmt.Errorf("bad reset payload: %w", jerr)
			}
			if herr := handle(ev, &rp, nil); herr != nil {
				return received, herr
			}
		case "error":
			var ep errPayload
			_ = json.Unmarshal([]byte(ev.Data), &ep)
			if herr := handle(ev, nil, &ep); herr != nil {
				return received, herr
			}
		default:
			if herr := handle(ev, nil, nil); herr != nil {
				return received, herr
			}
		}
	}
}

func publishCmd(args []string) {
	fs := flag.NewFlagSet("publish", flag.ExitOnError)
	addr := fs.String("addr", "http://127.0.0.1:8080", "server base URL")
	_ = fs.Parse(args)
	if fs.NArg() != 1 {
		usage()
	}
	payload, _ := json.Marshal(map[string]string{"data": fs.Arg(0)})
	req, err := http.NewRequest(http.MethodPost, *addr+"/events", bytes.NewReader(payload))
	if err != nil {
		log.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Fatalf("publish: %v", err)
	}
	defer resp.Body.Close()
	out := bufio.NewWriter(os.Stdout)
	defer out.Flush()
	fmt.Fprintf(out, "POST /events -> %s\n", resp.Status)
	_, _ = io.Copy(out, resp.Body)
	out.WriteByte('\n')
}
