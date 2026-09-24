// Package httpapi exposes received messages over HTTP using only net/http.
// The server must be bound to a loopback address.
package httpapi

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strconv"
	"strings"

	"smtprecv/internal/store"
)

// NewHandler builds the API handler backed by st.
func NewHandler(st *store.Store) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", handleHealth)
	mux.HandleFunc("/messages", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			handleList(st, w, r)
		default:
			methodNotAllowed(w, "GET")
		}
	})
	mux.HandleFunc("/messages/", func(w http.ResponseWriter, r *http.Request) {
		id, action, ok := parseMessagePath(r.URL.Path)
		if !ok || !validID(id) {
			writeJSON(w, http.StatusNotFound, errorBody("not found"))
			return
		}
		switch {
		case action == "" && r.Method == http.MethodGet:
			handleGet(st, w, r, id)
		case action == "raw" && r.Method == http.MethodGet:
			handleRaw(st, w, r, id)
		case action == "" && r.Method == http.MethodDelete:
			handleDelete(st, w, id)
		default:
			methodNotAllowed(w, "GET, DELETE")
		}
	})
	return mux
}

// RequireLoopback validates that host is a loopback address.
func RequireLoopback(addr string) error {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return fmt.Errorf("invalid listen address %q: %w", addr, err)
	}
	if host == "" || host == "localhost" {
		return nil
	}
	ip := net.ParseIP(host)
	if ip != nil && ip.IsLoopback() {
		return nil
	}
	return fmt.Errorf("refusing to listen on non-loopback address %s", host)
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w, "GET")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func handleList(st *store.Store, w http.ResponseWriter, r *http.Request) {
	limit := 0
	if v := r.URL.Query().Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			writeJSON(w, http.StatusBadRequest, errorBody("limit must be a non-negative integer"))
			return
		}
		limit = n
	}
	msgs := st.List(limit)
	writeJSON(w, http.StatusOK, map[string]any{"messages": msgs, "count": len(msgs)})
}

func handleGet(st *store.Store, w http.ResponseWriter, r *http.Request, id string) {
	m, err := st.Get(id)
	if errors.Is(err, store.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, errorBody("message not found"))
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorBody(err.Error()))
		return
	}
	writeJSON(w, http.StatusOK, m)
}

func handleRaw(st *store.Store, w http.ResponseWriter, r *http.Request, id string) {
	m, err := st.Get(id)
	if errors.Is(err, store.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, errorBody("message not found"))
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorBody(err.Error()))
		return
	}
	w.Header().Set("Content-Type", "message/rfc822")
	w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=%q", id+".eml"))
	w.WriteHeader(http.StatusOK)
	fmt.Fprint(w, m.Data)
}

func handleDelete(st *store.Store, w http.ResponseWriter, id string) {
	removed, err := st.Delete(id)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorBody(err.Error()))
		return
	}
	if !removed {
		writeJSON(w, http.StatusNotFound, errorBody("message not found"))
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "deleted", "id": id})
}

func parseMessagePath(p string) (id, action string, ok bool) {
	rest := strings.TrimPrefix(p, "/messages/")
	if rest == "" || strings.Contains(rest, "/") {
		parts := strings.Split(rest, "/")
		if len(parts) == 2 && parts[1] == "raw" && parts[0] != "" {
			return parts[0], "raw", true
		}
		return "", "", false
	}
	return rest, "", true
}

// validID guards against path traversal / odd file names. Store IDs are
// "<unixnanos>-<24 hex chars>".
func validID(id string) bool {
	if len(id) < 20 || len(id) > 80 {
		return false
	}
	dash := strings.IndexByte(id, '-')
	if dash <= 0 {
		return false
	}
	for _, ch := range id[:dash] {
		if ch < '0' || ch > '9' {
			return false
		}
	}
	for _, ch := range id[dash+1:] {
		if !((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f')) {
			return false
		}
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(body)
}

func errorBody(msg string) map[string]string {
	return map[string]string{"error": msg}
}

func methodNotAllowed(w http.ResponseWriter, allow string) {
	w.Header().Set("Allow", allow)
	writeJSON(w, http.StatusMethodNotAllowed, errorBody("method not allowed"))
}
