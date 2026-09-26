// Package client is a streaming test client with fault injection: it can
// read slowly (backpressure trigger), disconnect after N records, and it
// always validates the received sequence, emitting a structured JSON result.
package client

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net/http"
	"time"

	"streambp/internal/protocol"
)

// Options controls one client run.
type Options struct {
	URL string
	// ReadDelay is inserted before each record read (slow consumer).
	ReadDelay time.Duration
	// DisconnectAfter closes the connection after this many records
	// (data or terminal). Zero disables.
	DisconnectAfter int
	// Timeout bounds the whole run. Zero means no extra timeout.
	Timeout time.Duration
}

// Result is the structured outcome of a run, printed as JSON by the CLI.
type Result struct {
	URL          string `json:"url"`
	HTTPStatus   int    `json:"httpStatus"`
	Received     int    `json:"received"`
	EndStatus    string `json:"endStatus"` // "ok", "error", or "none"
	ErrorCode    string `json:"errorCode,omitempty"`
	ErrorMessage string `json:"errorMessage,omitempty"`
	ServerSent   int    `json:"serverSent"` // sent count reported by terminal record
	SequenceOK   bool   `json:"sequenceOk"`
	Disconnected bool   `json:"disconnected"`
	DurationMs   int64  `json:"durationMs"`
	Failure      string `json:"failure,omitempty"`
}

// Run executes one streaming request and returns its structured result.
func Run(ctx context.Context, opts Options) Result {
	start := time.Now()
	res := Result{URL: opts.URL, EndStatus: "none", SequenceOK: true}
	if opts.Timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, opts.Timeout)
		defer cancel()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, opts.URL, nil)
	if err != nil {
		res.Failure = err.Error()
		return res
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		res.Failure = err.Error()
		res.DurationMs = time.Since(start).Milliseconds()
		return res
	}
	defer resp.Body.Close()
	res.HTTPStatus = resp.StatusCode
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		res.Failure = fmt.Sprintf("http %d: %s", resp.StatusCode, string(body))
		res.DurationMs = time.Since(start).Milliseconds()
		return res
	}

	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 0, 64<<10), 16<<20)
	records := 0
	nextSeq := 1
	for sc.Scan() {
		if opts.ReadDelay > 0 {
			select {
			case <-time.After(opts.ReadDelay):
			case <-ctx.Done():
			}
		}
		rec, err := protocol.Decode(sc.Bytes())
		if err != nil {
			res.SequenceOK = false
			res.Failure = err.Error()
			break
		}
		records++
		switch rec.Type {
		case protocol.TypeData:
			if rec.Seq != nextSeq {
				res.SequenceOK = false
				res.Failure = fmt.Sprintf("seq gap: got %d want %d", rec.Seq, nextSeq)
			}
			nextSeq = rec.Seq + 1
			res.Received++
		case protocol.TypeEnd:
			res.EndStatus = rec.Status
			res.ServerSent = rec.Sent
		case protocol.TypeError:
			res.EndStatus = "error"
			res.ErrorCode = rec.Code
			res.ErrorMessage = rec.Message
			res.ServerSent = rec.Sent
		}
		if opts.DisconnectAfter > 0 && records >= opts.DisconnectAfter {
			res.Disconnected = true
			break
		}
	}
	if err := sc.Err(); err != nil && !res.Disconnected && ctx.Err() == nil {
		res.Failure = fmt.Sprintf("read stream: %v", err)
	}
	if ctx.Err() != nil && res.Failure == "" && !res.Disconnected {
		res.Failure = "deadline exceeded"
	}
	res.DurationMs = time.Since(start).Milliseconds()
	return res
}
