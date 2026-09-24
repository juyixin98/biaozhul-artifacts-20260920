// Command demo-client is a reference SSE consumer that demonstrates the
// resume protocol: Last-Event-ID reconnect, id-based deduplication, gap
// detection, heartbeats, and reset-triggered full resynchronization.
package main

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type frame struct {
	id      string
	event   string
	data    string
	comment bool
}

var errResetResync = errors.New("reset: full resync requested")

func main() {
	base := flag.String("url", "http://127.0.0.1:8080", "server base URL")
	duration := flag.Duration("duration", 0, "auto-exit after this duration (0 = until Ctrl-C)")
	startID := flag.Int64("last-event-id", 0, "initial Last-Event-ID (0 = from oldest retained)")
	lag := flag.Int("lag", 0, "send a cursor this many events behind the last received id on every reconnect (forces seam duplicates to demonstrate dedup)")
	showPings := flag.Bool("show-pings", false, "log heartbeat comments")
	maxReconnects := flag.Int("max-reconnects", 1000, "abort after this many reconnects")
	flag.Parse()

	c := &client{
		base:      strings.TrimRight(*base, "/"),
		lastID:    *startID,
		startID:   *startID,
		lag:       *lag,
		showPings: *showPings,
		seen:      make(map[int64]struct{}),
		maxReconn: *maxReconnects,
	}

	ctx, cancel := context.WithCancel(context.Background())
	if *duration > 0 {
		ctx, cancel = context.WithTimeout(context.Background(), *duration)
	}
	defer cancel()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	done := make(chan struct{})
	go func() {
		c.run(ctx)
		close(done)
	}()

	select {
	case <-done:
	case <-sigCh:
		cancel()
		<-done
	case <-ctx.Done():
		<-done
	}

	c.report()
}

type client struct {
	base       string
	lastID     int64 // high-water cursor
	startID    int64
	lag        int // intentionally lag the cursor on reconnect to force seam dups
	showPings  bool
	seen       map[int64]struct{}
	minSeen    int64
	maxSeen    int64
	unique     int
	duplicates int
	resyncs    int
	reconnects int
	maxReconn  int
	pings      int
	streamGaps int
}

func (c *client) run(ctx context.Context) {
	backoff := 200 * time.Millisecond
	for {
		if ctx.Err() != nil {
			return
		}
		err := c.connectOnce(ctx)
		if ctx.Err() != nil {
			return
		}
		if errors.Is(err, errResetResync) {
			// Cursor discarded; reopen from scratch immediately.
			backoff = 200 * time.Millisecond
			continue
		}
		c.reconnects++
		if c.reconnects > c.maxReconn {
			log.Printf("giving up after %d reconnects", c.reconnects-1)
			return
		}
		if err != nil {
			log.Printf("connection ended: %v; reconnecting with Last-Event-ID: %d in %s",
				err, c.lastID, backoff)
		} else {
			log.Printf("server closed stream; reconnecting with Last-Event-ID: %d in %s",
				c.lastID, backoff)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		if backoff < 2*time.Second {
			backoff *= 2
		}
	}
}

func (c *client) cursorForConnect() int64 {
	id := c.lastID
	if c.lag > 0 {
		id -= int64(c.lag)
		if id < 0 {
			id = 0
		}
	}
	return id
}

func (c *client) connectOnce(ctx context.Context) error {
	cursor := c.cursorForConnect()
	url := fmt.Sprintf("%s/v1/events/stream", c.base)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "text/event-stream")
	if cursor > 0 {
		req.Header.Set("Last-Event-ID", strconv.FormatInt(cursor, 10))
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("unexpected status %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	log.Printf("connected (sent Last-Event-ID=%d, high-water=%d)", cursor, c.lastID)

	return c.parseStream(resp.Body)
}

// parseStream reads blank-line-separated SSE dispatch blocks. Frames inside
// one stream are strictly ascending, which lets us detect in-stream gaps.
func (c *client) parseStream(r io.Reader) error {
	br := bufio.NewReaderSize(r, 64*1024)
	var block bytes.Buffer
	var prevInConn int64 // 0 = no id frame yet on this stream

	flush := func() error {
		if block.Len() == 0 {
			return nil
		}
		f := parseBlock(block.String())
		block.Reset()
		return c.handle(f, &prevInConn)
	}

	for {
		line, err := br.ReadString('\n')
		if len(line) > 0 {
			trimmed := strings.TrimRight(line, "\r\n")
			if trimmed == "" {
				if ferr := flush(); ferr != nil {
					return ferr
				}
			} else {
				block.WriteString(trimmed)
				block.WriteByte('\n')
			}
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				_ = flush()
				return nil
			}
			return err
		}
	}
}

func parseBlock(s string) frame {
	var f frame
	var dataLines []string
	commentOnly := true
	for _, line := range strings.Split(s, "\n") {
		if line == "" {
			continue
		}
		if strings.HasPrefix(line, ":") {
			continue
		}
		commentOnly = false
		field := line
		value := ""
		if i := strings.IndexByte(line, ':'); i >= 0 {
			field = line[:i]
			value = strings.TrimPrefix(line[i+1:], " ")
		}
		switch field {
		case "id":
			f.id = value
		case "event":
			f.event = value
		case "data":
			dataLines = append(dataLines, value)
		}
	}
	if f.id == "" && f.event == "" && len(dataLines) == 0 && commentOnly {
		f.comment = true
	}
	f.data = strings.Join(dataLines, "\n")
	return f
}

func (c *client) handle(f frame, prevInConn *int64) error {
	if f.comment {
		c.pings++
		if c.showPings {
			log.Printf("ping")
		}
		return nil
	}
	if f.id == "" {
		// Control frame (no id: line, so lastEventId is not advanced).
		switch f.event {
		case "reset":
			c.resyncs++
			log.Printf("*** RESET: %s", strings.ReplaceAll(f.data, "\n", " | "))
			log.Printf("*** protocol action: FULL RESYNC (next reconnect sends no cursor)")
			c.lastID = 0
			*prevInConn = 0
			return errResetResync
		case "ready":
			log.Printf("[ready] %s", f.data)
		case "slow_consumer":
			log.Printf("[slow_consumer] %s", f.data)
		default:
			log.Printf("[%s] %s", f.event, f.data)
		}
		return nil
	}

	id, err := strconv.ParseInt(f.id, 10, 64)
	if err != nil {
		log.Printf("ignoring frame with non-numeric id %q", f.id)
		return nil
	}

	if _, dup := c.seen[id]; dup {
		c.duplicates++
		log.Printf("dup  id=%-4d (redelivered at replay/live seam; discarded)", id)
	} else {
		c.seen[id] = struct{}{}
		c.unique++
		if c.unique == 1 {
			c.minSeen = id
		}
		c.maxSeen = id
		if *prevInConn != 0 && id != *prevInConn+1 {
			c.streamGaps++
			log.Printf("GAP  in stream: expected %d got %d", *prevInConn+1, id)
		}
		preview := f.data
		if len(preview) > 60 {
			preview = preview[:60] + "..."
		}
		preview = strings.ReplaceAll(preview, "\n", "\\n")
		log.Printf("recv id=%-4d event=%s data=%q", id, f.event, preview)
	}
	*prevInConn = id
	c.lastID = id
	return nil
}

func (c *client) report() {
	log.Printf("---------------- client report ----------------")
	log.Printf("unique events : %d", c.unique)
	log.Printf("duplicates    : %d (dropped after id-based dedup)", c.duplicates)
	log.Printf("id range seen : %d..%d", c.minSeen, c.maxSeen)
	log.Printf("reconnects    : %d", c.reconnects)
	log.Printf("resets/resync : %d", c.resyncs)
	log.Printf("pings         : %d", c.pings)
	log.Printf("in-stream gaps: %d", c.streamGaps)

	// Gap check is scoped to the observed window [minSeen, maxSeen]: ids
	// before minSeen may legitimately predate retention (the server would
	// have sent a reset otherwise). What must never happen is a hole inside
	// what this client actually received.
	var missing []int64
	for i := c.minSeen + 1; i < c.maxSeen; i++ {
		if _, ok := c.seen[i]; !ok {
			missing = append(missing, i)
		}
	}
	sort.Slice(missing, func(a, b int) bool { return missing[a] < missing[b] })
	switch {
	case len(missing) == 0:
		log.Printf("RESULT        : PASS - after dedup the observed id set is contiguous (no gaps)")
	case len(missing) <= 20:
		log.Printf("RESULT        : FAIL - missing ids: %v", missing)
	default:
		log.Printf("RESULT        : FAIL - %d missing ids, first 20: %v", len(missing), missing[:20])
	}
	log.Printf("-----------------------------------------------")
}
