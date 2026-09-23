// Package server wires the frame parser to a small net/http API.
//
// The service is offline by construction: it only parses bytes posted to it
// and never dials or proxies to any upstream server.
package server

import (
	"encoding/base64"
	"encoding/json"
	"net/http"

	"httpframe/frame"
)

// Config holds service limits.
type Config struct {
	MaxBody       int64 // parser entity-body limit
	MaxInputBytes int64 // wrapper request body limit
}

// DefaultConfig returns the built-in limits (10 MiB entity body, 16 MiB input).
func DefaultConfig() Config {
	return Config{MaxBody: 10 << 20, MaxInputBytes: 16 << 20}
}

type errBody struct {
	Error  string `json:"error"`
	Code   string `json:"code"`
	Offset int    `json:"offset"`
}

// New builds the http.Handler.
func New(cfg Config) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("/parse", parseHandler(cfg))
	mux.HandleFunc("/verify-splits", verifySplitsHandler(cfg))
	return mux
}

type headerDTO struct {
	Name      string `json:"name"`
	Value     string `json:"value"`
	LineStart int    `json:"line_start"`
}

type chunkDTO struct {
	Index         int    `json:"index"`
	Size          uint64 `json:"size"`
	Extension     string `json:"extension"`
	SizeLineStart int    `json:"size_line_start"`
	DataBase64    string `json:"data_base64"`
	DataStart     int    `json:"data_start"`
}

type messageDTO struct {
	Method      string      `json:"method"`
	Target      string      `json:"target"`
	Proto       string      `json:"proto"`
	Start       int         `json:"start"`
	Headers     []headerDTO `json:"headers"`
	HeaderStart int         `json:"header_start"`
	HeaderEnd   int         `json:"header_end"`
	Chunked     bool        `json:"chunked"`
	BodyBase64  string      `json:"body_base64"`
	BodyLength  int64       `json:"body_length"`
	BodyStart   int         `json:"body_start"`
	Chunks      []chunkDTO  `json:"chunks,omitempty"`
	Trailers    []headerDTO `json:"trailers,omitempty"`
	End         int         `json:"end"`
}

func toDTO(m *frame.Message) messageDTO {
	d := messageDTO{
		Method:      m.Method,
		Target:      m.Target,
		Proto:       m.Proto,
		Start:       m.Start,
		HeaderStart: m.HeaderStart,
		HeaderEnd:   m.HeaderEnd,
		Chunked:     m.Chunked,
		BodyBase64:  base64.StdEncoding.EncodeToString(m.Body),
		BodyLength:  m.BodyLength,
		BodyStart:   m.BodyStart,
		End:         m.End,
		Headers:     []headerDTO{},
		Chunks:      []chunkDTO{},
		Trailers:    []headerDTO{},
	}
	for _, h := range m.Headers {
		d.Headers = append(d.Headers, headerDTO{Name: h.Name, Value: h.Value, LineStart: h.LineStart})
	}
	for _, c := range m.Chunks {
		d.Chunks = append(d.Chunks, chunkDTO{
			Index:         c.Index,
			Size:          c.Size,
			Extension:     c.Extension,
			SizeLineStart: c.SizeLineStart,
			DataBase64:    base64.StdEncoding.EncodeToString(c.Data),
			DataStart:     c.DataStart,
		})
	}
	for _, h := range m.Trailers {
		d.Trailers = append(d.Trailers, headerDTO{Name: h.Name, Value: h.Value, LineStart: h.LineStart})
	}
	return d
}

func writeFrameError(w http.ResponseWriter, status int, e *frame.Error) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(errBody{Error: e.Msg, Code: e.Code, Offset: e.Offset})
}

func readWrapper(w http.ResponseWriter, r *http.Request, cfg Config) ([]byte, bool) {
	defer r.Body.Close()
	data, err := readAllCapped(r.Body, cfg.MaxInputBytes)
	if err != nil {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusRequestEntityTooLarge)
		_ = json.NewEncoder(w).Encode(errBody{Error: err.Error(), Code: "input_too_large"})
		return nil, false
	}
	return data, true
}

func parseHandler(cfg Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			_ = json.NewEncoder(w).Encode(errBody{Error: "use POST"})
			return
		}
		data, ok := readWrapper(w, r, cfg)
		if !ok {
			return
		}
		opts := []frame.Option{frame.WithMaxBody(cfg.MaxBody)}
		if r.URL.Query().Get("pipeline") == "1" {
			msgs, e := frame.ParsePipeline(data, opts...)
			if e != nil {
				writeFrameError(w, http.StatusUnprocessableEntity, e)
				return
			}
			out := make([]messageDTO, 0, len(msgs))
			for _, m := range msgs {
				out = append(out, toDTO(m))
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"messages": out, "count": len(out)})
			return
		}
		m, e := frame.Parse(data, opts...)
		if e != nil {
			writeFrameError(w, http.StatusUnprocessableEntity, e)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"messages": []messageDTO{toDTO(m)}, "count": 1})
	}
}

func verifySplitsHandler(cfg Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			_ = json.NewEncoder(w).Encode(errBody{Error: "use POST"})
			return
		}
		data, ok := readWrapper(w, r, cfg)
		if !ok {
			return
		}
		rep := frame.VerifySplits(data, frame.WithMaxBody(cfg.MaxBody))
		status := http.StatusOK
		if !rep.OK {
			status = http.StatusConflict
		}
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(rep)
	}
}
