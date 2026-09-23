// Package server wires the strict RESP codec and the KV store onto an
// net/http server. The entire implementation uses only the standard library.
//
// Endpoints:
//
//	POST /resp      body = one or more pipelined RESP2 commands (an array of
//	                bulk strings per command); response = the pipelined replies.
//	GET  /healthz   liveness probe.
//	GET  /commands  JSON command table.
package server

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"respd/internal/kv"
	"respd/internal/resp"
)

// DefaultMaxBodyBytes caps a single POST /resp request at 64 MiB.
const DefaultMaxBodyBytes = 64 << 20

// Config configures New.
type Config struct {
	Store        *kv.Store
	MaxBodyBytes int64 // <= 0 means DefaultMaxBodyBytes
}

// New builds the http.Handler.
func New(cfg Config) http.Handler {
	if cfg.Store == nil {
		cfg.Store = kv.NewStore()
	}
	if cfg.MaxBodyBytes <= 0 {
		cfg.MaxBodyBytes = DefaultMaxBodyBytes
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/resp", handleResp(cfg.Store, cfg.MaxBodyBytes))
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = io.WriteString(w, "ok\n")
	})
	mux.HandleFunc("/commands", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_ = json.NewEncoder(w).Encode(kv.Commands())
	})
	return mux
}

func handleResp(store *kv.Store, maxBody int64) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", "POST")
			http.Error(w, "method not allowed: use POST /resp with RESP2 in the body", http.StatusMethodNotAllowed)
			return
		}

		var out bytes.Buffer
		ww := resp.NewWriter(&out)
		reader := resp.NewReader(http.MaxBytesReader(w, r.Body, maxBody))
		session := kv.NewSession(store)

		for {
			v, err := reader.ReadMessage()
			if errors.Is(err, io.EOF) {
				break // clean boundary between frames: request fully consumed
			}
			if err != nil {
				// Half packet (ErrUnexpectedEOF) or malformed framing
				// (ErrProtocol), as well as an oversized body from
				// MaxBytesReader, are reported in-band as RESP errors so a
				// batch client keeps one reply per request semantics. Any
				// replies already produced for earlier pipelined commands
				// are preserved and sent first.
				status := http.StatusOK
				var mbErr *http.MaxBytesError
				if errors.As(err, &mbErr) {
					status = http.StatusRequestEntityTooLarge
				}
				writeRESP(w, status, &out, resp.ErrVal("ERR", describe(err)))
				return
			}

			args, perr := commandArgs(v)
			if perr != nil {
				// Wrong outer frame, e.g. "+PING\r\n" instead of an array.
				_ = ww.WriteValue(perr)
				continue
			}
			_ = ww.WriteValue(session.Dispatch(args))
		}

		if out.Len() == 0 {
			// Empty body: nothing was asked.
			w.WriteHeader(http.StatusNoContent)
			return
		}
		writeRESP(w, http.StatusOK, &out, nil)
	}
}

func writeRESP(w http.ResponseWriter, status int, prefix *bytes.Buffer, last *resp.Value) {
	w.Header().Set("Content-Type", "application/octet-stream")
	w.WriteHeader(status)
	if prefix.Len() > 0 {
		_, _ = w.Write(prefix.Bytes())
	}
	if last != nil {
		_ = resp.NewWriter(w).WriteValue(last)
	}
}

// commandArgs validates that v is a non-empty client request: an array of at
// least one bulk/simple-string element. Returning an error *reply* (rather than
// tearing down the stream) mirrors how an inline-ish but structurally wrong
// frame is treated: one bad pipelined item must not kill its neighbours.
func commandArgs(v *resp.Value) ([]string, *resp.Value) {
	if v.Type != resp.Array {
		return nil, resp.ErrVal("ERR", "expected an array of bulk strings")
	}
	if len(v.Array) == 0 {
		return nil, resp.ErrVal("ERR", "empty command array")
	}
	args := make([]string, len(v.Array))
	for i, el := range v.Array {
		switch el.Type {
		case resp.BulkString:
			args[i] = string(el.Str)
		case resp.SimpleString:
			args[i] = el.Text
		default:
			return nil, resp.ErrVal("ERR", "command elements must be bulk strings")
		}
	}
	if args[0] == "" {
		return nil, resp.ErrVal("ERR", "empty command name")
	}
	return args, nil
}

func describe(err error) string {
	var mbErr *http.MaxBytesError
	if errors.As(err, &mbErr) {
		return "request body too large"
	}
	if errors.Is(err, resp.ErrUnexpectedEOF) {
		return "protocol error: unexpected EOF (half packet)"
	}
	if errors.Is(err, resp.ErrProtocol) {
		return err.Error()
	}
	return "protocol error: " + err.Error()
}
