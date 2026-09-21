package server

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"

	"signalboard/internal/db"
)

type ctxKey string

const screenCtxKey ctxKey = "screen"

func hashToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

func newScreenToken() (string, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return "sb_" + hex.EncodeToString(buf), nil
}

func screenFromCtx(r *http.Request) db.Screen {
	return r.Context().Value(screenCtxKey).(db.Screen)
}

// screenAuth authenticates a screen by its bearer token. A token only ever
// resolves to one screen of one store, so a screen can only read its own
// store's data.
func (s *Server) screenAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
		if !ok || token == "" {
			writeError(w, http.StatusUnauthorized, "missing bearer token")
			return
		}
		screen, err := s.q.GetScreenByTokenHash(r.Context(), hashToken(token))
		if err != nil {
			writeError(w, http.StatusUnauthorized, "invalid token")
			return
		}
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), screenCtxKey, screen)))
	})
}

type createScreenRequest struct {
	Name string `json:"name"`
}

func (s *Server) createScreen(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	var req createScreenRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Name == "" {
		writeError(w, http.StatusBadRequest, "name is required")
		return
	}
	if _, err := s.q.GetStore(r.Context(), storeID); isNoRows(err) {
		writeError(w, http.StatusNotFound, "store not found")
		return
	}
	token, err := newScreenToken()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	screen, err := s.q.CreateScreen(r.Context(), db.CreateScreenParams{
		StoreID:   storeID,
		Name:      req.Name,
		TokenHash: hashToken(token),
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	// The plaintext token is returned exactly once, here.
	writeJSON(w, http.StatusCreated, map[string]any{
		"id":         uuidString(screen.ID),
		"store_id":   uuidString(screen.StoreID),
		"name":       screen.Name,
		"token":      token,
		"created_at": screen.CreatedAt,
	})
}

func (s *Server) listScreens(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	rows, err := s.q.ListScreens(r.Context(), storeID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		online, _ := row.Online.(bool)
		var lastHeartbeat any
		if row.LastHeartbeatAt.Valid {
			lastHeartbeat = row.LastHeartbeatAt.Time
		}
		out = append(out, map[string]any{
			"id":                uuidString(row.ID),
			"name":              row.Name,
			"created_at":        row.CreatedAt,
			"last_heartbeat_at": lastHeartbeat,
			"online":            online, // heartbeat within the last 90 seconds
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"screens": out})
}

func (s *Server) heartbeat(w http.ResponseWriter, r *http.Request) {
	screen := screenFromCtx(r)
	if err := s.q.Heartbeat(r.Context(), screen.ID); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
