package server

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"unicode/utf8"

	"respd/internal/resp"
)

// Mux builds the HTTP handler tree.
func (s *Server) Mux() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/resp", s.handleRESP)
	mux.HandleFunc("/exec", s.handleExec)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"status":"ok"}`)
	})
	mux.HandleFunc("/", s.handleIndex)
	return logging(mux)
}

// handleIndex describes the surface; any unknown path gets 404 JSON.
func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeJSON(w, http.StatusNotFound, map[string]string{
			"error": "not found",
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"service": "respd — RESP2 pipeline server over HTTP",
		"endpoints": []map[string]string{
			{"method": "POST", "path": "/resp", "type": "application/octet-stream",
				"description": "one or more RESP2 command arrays in the body; replies are RESP2, in order"},
			{"method": "POST", "path": "/exec", "type": "application/json",
				"description": "JSON convenience endpoint: {\"session\":id, \"command\":[\"SET\",\"k\",\"v\"]}"},
			{"method": "GET", "path": "/healthz", "description": "liveness probe"},
		},
		"session_header": "X-Session: <id> keeps MULTI state and SELECTed DB across requests",
	})
}

// handleRESP is the raw RESP2 pipeline endpoint.
//
// The whole body is decoded before replies are sent, so the response can
// carry Content-Length. Every successfully parsed command produces one
// reply in order; if the body is cut mid-frame ("半包"), the status is 400
// and the body holds the replies produced so far followed by one terminal
// "-ERR Protocol error: ..." frame. A clean empty body replies 200 with an
// empty body.
func (s *Server) handleRESP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{
			"error": "method not allowed; use POST",
		})
		return
	}
	id, ok := sessionIDFromHeader(w, r)
	if !ok {
		return
	}

	limited := http.MaxBytesReader(w, r.Body, s.maxBody)
	body, err := io.ReadAll(limited)
	if err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{
				"error": fmt.Sprintf("request body exceeds limit of %d bytes", s.maxBody),
			})
			return
		}
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	sess := s.sessionFor(id)
	sess.mu.Lock()
	defer sess.mu.Unlock()

	dec := resp.NewDecoderLimits(bytes.NewReader(body), s.limits)
	var out bytes.Buffer
	enc := resp.NewEncoder(&out)
	hadCmd := false
	for {
		v, derr := dec.Next()
		if derr == io.EOF {
			break
		}
		if derr != nil {
			msg := protocolErrorMessage(derr)
			// A protocol error drops any open transaction on this session.
			sess.resetLocked()
			_ = enc.Encode(resp.SimpleError(msg))
			w.Header().Set("Content-Type", "application/octet-stream")
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write(out.Bytes())
			return
		}
		for _, reply := range s.dispatchLocked(sess, v, false) {
			_ = enc.Encode(reply)
		}
		hadCmd = true
	}
	if !hadCmd {
		// An empty/whitespace-only body is a legal pipeline of zero items.
		w.Header().Set("Content-Type", "application/octet-stream")
		w.WriteHeader(http.StatusOK)
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Length", fmt.Sprint(out.Len()))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(out.Bytes())
}

// protocolErrorMessage renders a decoder error as a safe single-line
// "-ERR" message.
func protocolErrorMessage(err error) string {
	pe := resp.AsProtocolError(err)
	msg := "invalid stream"
	if pe != nil {
		msg = pe.Error()
	} else {
		msg = err.Error()
	}
	msg = strings.NewReplacer("\r", " ", "\n", " ").Replace(msg)
	return "ERR Protocol error: " + msg
}

func sessionIDFromHeader(w http.ResponseWriter, r *http.Request) (string, bool) {
	id := strings.TrimSpace(r.Header.Get("X-Session"))
	if id == "" {
		return "", true
	}
	if len(id) > 128 {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "X-Session must be at most 128 bytes",
		})
		return "", false
	}
	for i := 0; i < len(id); i++ {
		c := id[i]
		if c < 0x21 || c > 0x7e {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": "X-Session must contain only printable ASCII",
			})
			return "", false
		}
	}
	return id, true
}

// execRequest is the JSON shape accepted by /exec.
type execRequest struct {
	Session string   `json:"session,omitempty"`
	Command []string `json:"command"`
}

func (s *Server) handleExec(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{
			"error": "method not allowed; use POST",
		})
		return
	}
	var req execRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, s.maxBody)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}
	if len(req.Command) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "command must be a non-empty array"})
		return
	}
	id, ok := sessionIDFromHeader(w, r)
	if !ok {
		return
	}
	if id == "" {
		id = strings.TrimSpace(req.Session)
		if id != "" {
			if len(id) > 128 {
				writeJSON(w, http.StatusBadRequest, map[string]string{
					"error": "session must be at most 128 bytes",
				})
				return
			}
			for i := 0; i < len(id); i++ {
				if id[i] < 0x21 || id[i] > 0x7e {
					writeJSON(w, http.StatusBadRequest, map[string]string{
						"error": "session must contain only printable ASCII",
					})
					return
				}
			}
		}
	}
	elems := make([]resp.Value, len(req.Command))
	for i, a := range req.Command {
		elems[i] = resp.BulkString(a)
	}

	sess := s.sessionFor(id)
	sess.mu.Lock()
	defer sess.mu.Unlock()
	reply := s.dispatchLocked(sess, resp.ArrayValue(elems...), false)[0]
	writeJSON(w, http.StatusOK, replyToJSON(reply))
}

// replyJSON is the JSON projection of one RESP2 reply.
type replyJSON struct {
	Type    string      `json:"type"`
	Value   string      `json:"value,omitempty"`
	Integer *int64      `json:"integer,omitempty"`
	Binary  string      `json:"binary_b64,omitempty"`
	Null    bool        `json:"null,omitempty"`
	Error   string      `json:"error,omitempty"`
	Values  []replyJSON `json:"values,omitempty"`
}

func replyToJSON(v resp.Value) replyJSON {
	out := replyJSON{}
	switch v.Kind {
	case resp.KindSimple:
		out.Type = "simple"
		out.Value = v.Str
	case resp.KindError:
		out.Type = "error"
		out.Error = v.Str
	case resp.KindInteger:
		out.Type = "integer"
		n := v.N
		out.Integer = &n
	case resp.KindBulk:
		out.Type = "bulk"
		if v.Bulk == nil {
			out.Null = true
		} else if utf8.Valid(v.Bulk) {
			out.Value = string(v.Bulk)
		} else {
			out.Binary = base64.StdEncoding.EncodeToString(v.Bulk)
		}
	case resp.KindArray:
		out.Type = "array"
		if v.Array == nil {
			out.Null = true
			break
		}
		out.Values = make([]replyJSON, len(v.Array))
		for i, e := range v.Array {
			out.Values[i] = replyToJSON(e)
		}
	}
	return out
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(body)
}
