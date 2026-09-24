// Package server exposes an hlc.Clock over HTTP using only net/http.
package server

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"

	"hlc-service/hlc"
)

// Server wires an *hlc.Clock into HTTP handlers.
type Server struct {
	Clock *hlc.Clock
}

// New creates a Server.
func New(c *hlc.Clock) *Server { return &Server{Clock: c} }

// Mux builds the handler with all routes registered.
func (s *Server) Mux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("POST /v1/tick", s.handleTick)
	mux.HandleFunc("GET /v1/now", s.handleNow)
	mux.HandleFunc("POST /v1/receive", s.handleReceive)
	mux.HandleFunc("GET /v1/status", s.handleStatus)
	return mux
}

type envelope struct {
	NodeID    string        `json:"node_id"`
	Timestamp hlc.Timestamp `json:"timestamp"`
}

type receiveRequest struct {
	Timestamp hlc.Timestamp `json:"timestamp"`
}

type errorResponse struct {
	Error  string `json:"error"`
	Code   string `json:"code"`
	Detail string `json:"detail,omitempty"`
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	st := s.Clock.Stats()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"status":          "ok",
		"node_id":         s.Clock.NodeID(),
		"physical_now_ms": st.PhysicalNow,
	})
}

func (s *Server) handleTick(w http.ResponseWriter, r *http.Request) {
	ts, err := s.Clock.Tick()
	if err != nil {
		writeClockError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, envelope{NodeID: s.Clock.NodeID(), Timestamp: ts})
}

func (s *Server) handleNow(w http.ResponseWriter, r *http.Request) {
	ts := s.Clock.Peek()
	writeJSON(w, http.StatusOK, envelope{NodeID: s.Clock.NodeID(), Timestamp: ts})
}

func (s *Server) handleReceive(w http.ResponseWriter, r *http.Request) {
	remote, ok := decodeTimestamp(w, r)
	if !ok {
		return
	}
	ts, err := s.Clock.Receive(remote)
	if err != nil {
		writeClockError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, envelope{NodeID: s.Clock.NodeID(), Timestamp: ts})
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.Clock.Stats())
}

// decodeTimestamp accepts three request shapes:
//
//	{"timestamp": { ... }}                 (envelope)
//	{"physical_ms":..,"logical":..,...}    (bare timestamp object)
//	"hlc://node/1700:3"                    (canonical text, JSON string)
//	hlc://node/1700:3                      (canonical text, raw body)
func decodeTimestamp(w http.ResponseWriter, r *http.Request) (hlc.Timestamp, bool) {
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return hlc.Timestamp{}, false
	}
	trimmed := strings.TrimSpace(string(body))
	if trimmed == "" {
		writeError(w, http.StatusBadRequest, "bad_request", "empty request body")
		return hlc.Timestamp{}, false
	}

	// Raw canonical text form (not JSON-quoted).
	if strings.HasPrefix(trimmed, hlc.WirePrefix) && !strings.HasPrefix(trimmed, `"`) {
		ts, err := hlc.ParseWire(trimmed)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid_timestamp", err.Error())
			return hlc.Timestamp{}, false
		}
		return ts, true
	}

	// JSON-quoted canonical text form: "hlc://node/p:l".
	if strings.HasPrefix(trimmed, `"`) {
		var s string
		if err := json.Unmarshal(body, &s); err != nil {
			writeError(w, http.StatusBadRequest, "bad_json", err.Error())
			return hlc.Timestamp{}, false
		}
		ts, err := hlc.ParseWire(s)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid_timestamp", err.Error())
			return hlc.Timestamp{}, false
		}
		return ts, true
	}

	// JSON object: either a bare timestamp object, or {"timestamp": ...}.
	var head map[string]json.RawMessage
	if err := json.Unmarshal(body, &head); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", err.Error())
		return hlc.Timestamp{}, false
	}
	var raw json.RawMessage
	if inner, ok := head["timestamp"]; ok {
		raw = inner
	} else {
		raw = json.RawMessage(body)
	}
	var ts hlc.Timestamp
	if err := json.Unmarshal(raw, &ts); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_timestamp", err.Error())
		return hlc.Timestamp{}, false
	}
	return ts, true
}

func writeClockError(w http.ResponseWriter, err error) {
	var drift *hlc.FutureDriftError
	switch {
	case errors.As(err, &drift):
		writeError(w, http.StatusUnprocessableEntity, "future_drift", err.Error())
	case errors.Is(err, hlc.ErrInvalidTimestamp):
		writeError(w, http.StatusBadRequest, "invalid_timestamp", err.Error())
	case errors.Is(err, hlc.ErrOverflow):
		writeError(w, http.StatusServiceUnavailable, "overflow", err.Error())
	default:
		writeError(w, http.StatusInternalServerError, "internal", err.Error())
	}
}

func writeError(w http.ResponseWriter, status int, code, detail string) {
	writeJSON(w, status, errorResponse{Error: http.StatusText(status), Code: code, Detail: detail})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
