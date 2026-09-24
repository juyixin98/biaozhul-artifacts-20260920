package server

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"

	"hlcservice/internal/hlc"
)

// Every timestamp is carried (and accepted) in these shapes:
//
//	{"timestamp": {"physical_ms":..,"logical":..,"node_id":".."}}  envelope
//	{"physical_ms":..,"logical":..,"node_id":".."}                 bare object
//	"hlc://node/1700:3"                                            JSON string
//	hlc://node/1700:3                                              raw text body
//
// Numeric fields are exact JSON integers; hlc.Timestamp also accepts quoted
// numbers and always emits the canonical "wire" string alongside them, so
// 64-bit millisecond values and counters near 2^32 round-trip losslessly.

type errorResponse struct {
	Error  string `json:"error"`
	Code   string `json:"code"`
	Detail string `json:"detail,omitempty"`
}

// decodeTimestamp parses any of the four accepted receive-body shapes.
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

	// Raw canonical text (not JSON-quoted).
	if strings.HasPrefix(trimmed, hlc.WirePrefix) && !strings.HasPrefix(trimmed, `"`) {
		ts, err := hlc.ParseWire(trimmed)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid_timestamp", err.Error())
			return hlc.Timestamp{}, false
		}
		return ts, true
	}

	// JSON-quoted canonical text: "hlc://node/p:l".
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

	// JSON object: either {"timestamp": {...}} or a bare timestamp object.
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

func (s *Server) handleReceive(w http.ResponseWriter, r *http.Request) {
	remote, ok := decodeTimestamp(w, r)
	if !ok {
		return
	}
	t, err := s.clock.Receive(remote)
	if err != nil {
		writeClockError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, s.envelope(t))
}

// writeClockError maps clock-domain errors to HTTP statuses and stable codes.
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
