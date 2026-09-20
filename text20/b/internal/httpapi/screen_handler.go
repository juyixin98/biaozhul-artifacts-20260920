package httpapi

import (
	"net/http"
	"strconv"
	"strings"
	"time"
)

// etagsMatch reports whether any of the comma-separated candidate validators
// in header matches etag. Supports "*" (any) and weak comparison is
// unnecessary here because we only emit strong validators.
func etagsMatch(header, etag string) bool {
	header = strings.TrimSpace(header)
	if header == "" {
		return false
	}
	if header == "*" {
		return true
	}
	for _, part := range strings.Split(header, ",") {
		if strings.TrimSpace(part) == etag {
			return true
		}
	}
	return false
}

// screenMenu serves the complete current menu to a screen.
//
// Conditional requests:
//   - If-None-Match: GET / HEAD with the current ETag -> 304 (no body).
//   - If-Match: the client's validator must match the current state, otherwise
//     412 (used after a rejected update / on reconnect to detect stale state).
//
// Because the ETag hashes the published version, every active temp-price line
// and every sellout flag, stale caches can never be served after a change.
func (s *Server) screenMenu(w http.ResponseWriter, r *http.Request) {
	screen := screenFromCtx(r)
	now := time.Now()

	menu, err := s.svc.GetScreenMenu(r.Context(), screen.StoreID, now)
	if err != nil {
		mapServiceError(w, err)
		return
	}
	etag := menu.ETag()
	w.Header().Set("ETag", etag)
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Last-Modified", menu.GeneratedAt.UTC().Format(http.TimeFormat))
	w.Header().Set("X-Store-Id", strconv.FormatInt(screen.StoreID, 10))

	if ifMatch := r.Header.Get("If-Match"); ifMatch != "" && !etagsMatch(ifMatch, etag) {
		w.WriteHeader(http.StatusPreconditionFailed)
		return
	}
	if inm := r.Header.Get("If-None-Match"); inm != "" && etagsMatch(inm, etag) {
		w.WriteHeader(http.StatusNotModified)
		return
	}

	if r.Method == http.MethodHead {
		w.WriteHeader(http.StatusOK)
		return
	}
	writeJSON(w, http.StatusOK, menu)
}

// screenHeartbeat is called by screens on a short interval. 90 seconds after
// the last call the screen counts as offline.
func (s *Server) screenHeartbeat(w http.ResponseWriter, r *http.Request) {
	screen := screenFromCtx(r)
	online, err := s.svc.Heartbeat(r.Context(), screen.ID)
	if err != nil {
		mapServiceError(w, err)
		return
	}
	// After reconnecting (offline -> online), the screen must fetch the full
	// latest menu. Report whether it was offline so the client knows to
	// re-pull; the full menu endpoint always returns the current snapshot.
	wasOffline := time.Since(screen.LastSeenAt.Time) > 90*time.Second
	writeJSON(w, http.StatusOK, map[string]bool{
		"online": online, "reconnect": wasOffline,
	})
}
