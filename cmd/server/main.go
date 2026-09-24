// Command framesrv is an offline HTTP/1.1 request-framing consistency
// service. It only parses and validates raw request byte streams posted by
// clients; it never opens outbound connections or forwards traffic.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"

	"framesrv/internal/parser"
)

type server struct {
	maxBody       int64 // decoded body limit enforced by the parser
	maxCarrier    int64 // max bytes accepted as a /parse carrier payload
	maxVerifySize int   // max bytes accepted by /verify (exhaustive splits are O(n^2))
}

type parseResponse struct {
	OK           bool              `json:"ok"`
	InputBytes   int               `json:"input_bytes"`
	RequestCount int               `json:"request_count,omitempty"`
	Requests     []*parser.Request `json:"requests,omitempty"`
	Error        *parser.Error     `json:"error,omitempty"`
}

type verifyResponse struct {
	OK         bool                  `json:"ok"`
	InputBytes int                   `json:"input_bytes"`
	Splits     int                   `json:"splits_checked"`
	Error      *parser.Error         `json:"parse_error,omitempty"`
	Mismatch   *parser.SplitMismatch `json:"mismatch,omitempty"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *server) handleParse(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{
			"error": "use POST with a raw HTTP/1.1 request byte stream as the body",
		})
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, s.maxCarrier)
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{
			"error": fmt.Sprintf("carrier payload too large (limit %d bytes): %v", s.maxCarrier, err),
		})
		return
	}
	if len(raw) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "empty body: post a raw HTTP/1.1 request byte stream",
		})
		return
	}

	p := parser.NewParser(parser.WithMaxBody(s.maxBody))
	completed, perr := p.Feed(raw)
	if perr == nil {
		perr = p.Finish()
	}
	// p.Requests() includes requests completed in earlier Feed turns too;
	// here the whole input was fed at once so it equals completed.
	reqs := p.Requests()
	if completed != nil && len(reqs) == 0 {
		reqs = completed
	}

	resp := parseResponse{
		OK:           perr == nil,
		InputBytes:   len(raw),
		RequestCount: len(reqs),
		Requests:     reqs,
		Error:        perr,
	}
	// Parser failures are analysis results, not server faults: always 200.
	writeJSON(w, http.StatusOK, resp)
}

func (s *server) handleVerify(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{
			"error": "use POST with a raw HTTP/1.1 request byte stream as the body",
		})
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, int64(s.maxVerifySize))
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{
			"error": fmt.Sprintf("verify payload too large (limit %d bytes)", s.maxVerifySize),
		})
		return
	}
	if len(raw) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "empty body: post a raw HTTP/1.1 request byte stream",
		})
		return
	}

	opts := []parser.Option{parser.WithMaxBody(s.maxBody)}
	_, perr := parser.Parse(raw, opts...)
	mismatch := parser.VerifyAllSplits(raw, opts...)
	resp := verifyResponse{
		OK:         mismatch == nil,
		InputBytes: len(raw),
		Splits:     len(raw) + 1,
		Error:      perr,
		Mismatch:   mismatch,
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *server) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	fmt.Fprintln(w, `HTTP framing consistency service (offline, no forwarding)

POST /parse   - body is a raw HTTP/1.1 request byte stream (pipelined allowed);
                returns parsed framing or an error with an exact byte offset
POST /verify  - same input; exhaustively checks every 2-way byte split and
                reports the first split whose result differs from one-shot parse
GET  /healthz - liveness probe`)
}

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	maxBody := flag.Int64("maxbody", 10<<20, "maximum decoded request body in bytes (0 = unlimited)")
	maxCarrier := flag.Int64("maxcarrier", 1<<20, "maximum /parse carrier payload in bytes")
	maxVerify := flag.Int("maxverify", 4096, "maximum /verify payload in bytes (O(n^2) check)")
	flag.Parse()

	s := &server{maxBody: *maxBody, maxCarrier: *maxCarrier, maxVerifySize: *maxVerify}
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.handleIndex)
	mux.HandleFunc("/parse", s.handleParse)
	mux.HandleFunc("/verify", s.handleVerify)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	log.Printf("HTTP framing service listening on %s (maxbody=%d)", *addr, *maxBody)
	if err := http.ListenAndServe(*addr, mux); err != nil {
		log.Fatal(err)
	}
}
