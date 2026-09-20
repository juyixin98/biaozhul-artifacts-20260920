package api

import (
	"net/http"

	"github.com/google/uuid"

	"sircc/internal/service"
)

// requestID extracts the client-supplied idempotency key. It is a UUID sent
// either as Idempotency-Key (preferred) or X-Request-ID. A malformed value
// yields the zero UUID, which simply disables idempotency for the call.
func requestID(r *http.Request) uuid.UUID {
	raw := r.Header.Get("Idempotency-Key")
	if raw == "" {
		raw = r.Header.Get("X-Request-ID")
	}
	if raw == "" {
		return uuid.Nil
	}
	id, err := uuid.Parse(raw)
	if err != nil {
		return uuid.Nil
	}
	return id
}

func renderResult(w http.ResponseWriter, res service.Result) {
	if res.Replay != nil {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Header().Set("Idempotent-Replay", "true")
		w.WriteHeader(res.Status)
		// Match json.Encoder's trailing newline so the replay is byte
		// identical to the original response.
		_, _ = w.Write(append(res.Replay, '\n'))
		return
	}
	writeJSON(w, res.Status, res.Body)
}
