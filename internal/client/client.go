// Package client is a fault-injection test client for the streaming
// service. It can consume a stream as a fast reader, a slow reader
// (delay between records), or a reader that disconnects mid-stream.
// Every run produces a structured Result for automated verification.
package client

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"syscall"
	"time"

	"streamback/internal/stream"
)

// Mode selects the fault-injection behavior.
type Mode string

const (
	// ModeFast reads records as fast as they arrive.
	ModeFast Mode = "fast"
	// ModeSlow sleeps ReadDelay between records, forcing the server to
	// buffer and apply backpressure.
	ModeSlow Mode = "slow"
	// ModeDisconnect closes the connection after DisconnectAfter records.
	ModeDisconnect Mode = "disconnect"
)

// Options parameterizes one client run.
type Options struct {
	// URL is the full /stream URL including query parameters.
	URL  string
	Mode Mode
	// ReadDelay is the per-read-chunk socket delay in slow mode. It
	// throttles how fast the kernel receive buffer is drained, which is
	// what actually propagates TCP backpressure to the server.
	ReadDelay time.Duration
	// DisconnectAfter is the number of records to read before closing
	// the connection in disconnect mode.
	DisconnectAfter int
	// MaxRecords is a safety cap on records read (0 = no cap).
	MaxRecords int
	// MaxLineBytes bounds a single protocol line accepted.
	MaxLineBytes int
	// ReceiveBufferBytes, when > 0, sets SO_RCVBUF on the connection.
	// A tiny receive buffer makes TCP-level backpressure visible at the
	// application level even on loopback, whose kernel buffers would
	// otherwise absorb the whole test stream.
	ReceiveBufferBytes int
	// HTTPClient allows injecting a custom client; nil uses a default.
	HTTPClient *http.Client
}

// readChunkBytes bounds how much a throttled read drains per delay tick.
const readChunkBytes = 4096

// throttleReader sleeps delay per Read and caps each Read at maxChunk
// bytes, so a slow consumer stops draining the kernel receive buffer and
// TCP flow control pushes back on the sender.
type throttleReader struct {
	r        io.Reader
	delay    time.Duration
	maxChunk int
}

func (t *throttleReader) Read(p []byte) (int, error) {
	if t.maxChunk > 0 && len(p) > t.maxChunk {
		p = p[:t.maxChunk]
	}
	time.Sleep(t.delay)
	return t.r.Read(p)
}

// Result is the structured outcome of one run.
type Result struct {
	Mode          Mode           `json:"mode"`
	URL           string         `json:"url"`
	StatusCode    int            `json:"statusCode"`
	ItemsReceived int            `json:"itemsReceived"`
	Seqs          []int          `json:"seqs"`
	Trailer       *stream.Record `json:"trailer,omitempty"`
	Completed     bool           `json:"completed"` // saw an "end" trailer
	ErrorCode     string         `json:"errorCode,omitempty"`
	ErrorMessage  string         `json:"errorMessage,omitempty"`
	Disconnected  bool           `json:"disconnected"` // closed early by our own fault injection
	DurationMs    int64          `json:"durationMs"`
	Failure       string         `json:"failure,omitempty"` // transport/protocol failure, if any
}

// Run executes one client run and returns its structured result.
// Run itself never fails fatally: transport and protocol problems are
// reported inside Result so test harnesses can assert on them.
func Run(ctx context.Context, opts Options) (res Result) {
	start := time.Now()
	res.Mode = opts.Mode
	res.URL = opts.URL

	hc := opts.HTTPClient
	if hc == nil {
		hc = &http.Client{}
	}
	if opts.ReceiveBufferBytes > 0 {
		dialer := &net.Dialer{}
		dialer.Control = func(_, _ string, c syscall.RawConn) error {
			var sockErr error
			err := c.Control(func(fd uintptr) {
				sockErr = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_RCVBUF, opts.ReceiveBufferBytes)
			})
			if err != nil {
				return err
			}
			return sockErr
		}
		hc = &http.Client{Transport: &http.Transport{DialContext: dialer.DialContext}}
	}
	maxLine := opts.MaxLineBytes
	if maxLine <= 0 {
		maxLine = 1024 * 1024
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, opts.URL, nil)
	if err != nil {
		res.Failure = fmt.Sprintf("build request: %v", err)
		return res
	}
	resp, err := hc.Do(req)
	if err != nil {
		res.Failure = fmt.Sprintf("do request: %v", err)
		return res
	}
	defer resp.Body.Close()
	res.StatusCode = resp.StatusCode

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		res.Failure = fmt.Sprintf("non-200 status %d: %s", resp.StatusCode, string(body))
		res.DurationMs = time.Since(start).Milliseconds()
		return res
	}

	var body io.Reader = resp.Body
	if opts.Mode == ModeSlow && opts.ReadDelay > 0 {
		body = &throttleReader{r: resp.Body, delay: opts.ReadDelay, maxChunk: readChunkBytes}
	}
	dec := stream.NewDecoder(body, maxLine)
	read := 0
	for {
		if opts.MaxRecords > 0 && read >= opts.MaxRecords {
			res.Failure = fmt.Sprintf("safety cap of %d records reached", opts.MaxRecords)
			break
		}
		if opts.Mode == ModeDisconnect && read >= opts.DisconnectAfter {
			res.Disconnected = true
			resp.Body.Close() // fault injection: abandon the stream
			break
		}

		rec, err := dec.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			res.Failure = err.Error()
			break
		}
		read++

		switch rec.Type {
		case stream.TypeItem:
			res.ItemsReceived++
			res.Seqs = append(res.Seqs, rec.Seq)
		case stream.TypeEnd:
			r := rec
			res.Trailer = &r
			res.Completed = true
		case stream.TypeError:
			r := rec
			res.Trailer = &r
			res.ErrorCode = rec.Code
			res.ErrorMessage = rec.Message
		default:
			res.Failure = fmt.Sprintf("unknown record type %q", rec.Type)
		}
		if res.Trailer != nil {
			break
		}
	}

	res.DurationMs = time.Since(start).Milliseconds()
	return res
}
