// Package httpapi exposes the received-mail store over a small net/http API.
// It is read-only with respect to SMTP state; deleting stored test mail is
// allowed so test suites can reset themselves.
package httpapi

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"loopmail/store"
)

// MessageSummary is the JSON representation used in list responses.
type MessageSummary struct {
	ID         string    `json:"id"`
	From       string    `json:"from"`
	To         []string  `json:"to"`
	Subject    string    `json:"subject"`
	Size       int       `json:"size"`
	ReceivedAt time.Time `json:"received_at"`
}

// Handler builds the HTTP handler backed by st.
func Handler(st *store.Store, log *slog.Logger) http.Handler {
	if log == nil {
		log = slog.Default()
	}
	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	mux.HandleFunc("GET /messages", func(w http.ResponseWriter, r *http.Request) {
		summaries := st.List()
		out := make([]MessageSummary, 0, len(summaries))
		for _, s := range summaries {
			// List() returns summaries without raw payloads; fetch the full
			// message so we can extract a Subject preview.
			m, err := st.Get(s.ID)
			if err != nil {
				writeError(w, http.StatusInternalServerError, err.Error())
				return
			}
			out = append(out, MessageSummary{
				ID:         m.ID,
				From:       m.From,
				To:         m.To,
				Subject:    extractSubject(m.Raw),
				Size:       m.Size,
				ReceivedAt: m.Received,
			})
		}
		writeJSON(w, http.StatusOK, map[string]any{"messages": out, "count": len(out)})
	})

	mux.HandleFunc("GET /messages/{id}", func(w http.ResponseWriter, r *http.Request) {
		m, err := st.Get(r.PathValue("id"))
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "message not found")
			return
		}
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		w.Header().Set("Content-Type", "message/rfc822")
		w.Header().Set("X-Mail-From", m.From)
		w.Header()["X-Mail-Rcpt"] = m.To
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(m.Raw)
	})

	mux.HandleFunc("DELETE /messages/{id}", func(w http.ResponseWriter, r *http.Request) {
		err := st.Delete(r.PathValue("id"))
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "message not found")
			return
		}
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})

	return logRequests(log, mux)
}

// extractSubject does a best-effort extraction of the Subject header from the
// (headers section of the) raw message. Only a single, un-folded ASCII/UTF-8
// value is returned; encoded-word decoding and header folding are
// intentionally out of scope for the test harness.
func extractSubject(raw []byte) string {
	// Headers run until the first blank line; scan within safe bounds.
	headers := raw
	if idx := indexCRLFCRLF(raw); idx >= 0 {
		headers = raw[:idx]
	}
	for _, line := range strings.Split(string(headers), "\n") {
		line = strings.TrimSuffix(line, "\r")
		// Folded continuation lines are ignored (no unfold).
		if line == "" || line[0] == ' ' || line[0] == '\t' {
			continue
		}
		colon := strings.IndexByte(line, ':')
		if colon < 0 {
			continue
		}
		if strings.EqualFold(strings.TrimSpace(line[:colon]), "Subject") {
			return strings.TrimSpace(line[colon+1:])
		}
	}
	return ""
}

// indexCRLFCRLF returns the byte index of the first CRLFCRLF sequence, or -1.
func indexCRLFCRLF(b []byte) int {
	for i := 0; i+4 <= len(b); i++ {
		if b[i] == '\r' && b[i+1] == '\n' && b[i+2] == '\r' && b[i+3] == '\n' {
			return i
		}
	}
	return -1
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func logRequests(log *slog.Logger, h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		h.ServeHTTP(sw, r)
		log.Info("http", "method", r.Method, "path", r.URL.Path, "status", sw.status, "dur", time.Since(start))
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}
