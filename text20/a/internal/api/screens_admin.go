package api

import (
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"time"

	"signalboard/internal/db"
	"signalboard/internal/httpx"
)

type createScreenReq struct {
	Name string `json:"name"`
}

// createScreen provisions a physical screen. A fresh opaque token is
// generated server side and returned exactly once; only its SHA-256 hash is
// stored. The token is bound to a single store and can read only that store.
func (s *Server) createScreen(w http.ResponseWriter, r *http.Request) {
	storeID, ok := urlID(w, r, "storeID")
	if !ok {
		return
	}
	if _, _, ok := s.loadStore(w, r, storeID); !ok {
		return
	}
	var req createScreenReq
	if !httpx.DecodeJSON(w, r, &req) {
		return
	}
	if req.Name == "" {
		httpx.ErrorJSON(w, http.StatusBadRequest, "invalid_screen", "name is required")
		return
	}

	raw, err := generateToken()
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	screen, err := s.q.CreateScreen(r.Context(), db.CreateScreenParams{
		StoreID:   storeID,
		Name:      req.Name,
		TokenHash: hashToken(raw),
	})
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}

	httpx.JSON(w, http.StatusCreated, map[string]any{
		"id":           screen.ID,
		"store_id":     screen.StoreID,
		"name":         screen.Name,
		"token":        raw, // shown only here
		"created_at":   tsTime(screen.CreatedAt).Format(time.RFC3339Nano),
		"authenticate": "send token as X-Screen-Token header or 'Authorization: Screen <token>'",
	})
}

func (s *Server) listScreens(w http.ResponseWriter, r *http.Request) {
	storeID, ok := urlID(w, r, "storeID")
	if !ok {
		return
	}
	if _, _, ok := s.loadStore(w, r, storeID); !ok {
		return
	}
	screens, err := s.q.ListScreens(r.Context(), storeID)
	if err != nil {
		httpx.ErrorJSON(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]map[string]any, 0, len(screens))
	for _, sc := range screens {
		out = append(out, s.screenStatusView(sc))
	}
	httpx.JSON(w, http.StatusOK, out)
}

// screenStatusView projects a screen plus its derived online state. A screen
// with no heartbeat yet is offline.
func (s *Server) screenStatusView(sc db.Screen) map[string]any {
	online := false
	var lastHB any
	var secondsSince int64 = -1
	if sc.LastHeartbeat.Valid {
		hb := sc.LastHeartbeat.Time.UTC()
		since := nowFn().Sub(hb)
		secondsSince = int64(since.Seconds())
		online = since <= s.cfg.ScreenOfflineAfter
		lastHB = hb.Format(time.RFC3339Nano)
	}
	return map[string]any{
		"id":                        sc.ID,
		"store_id":                  sc.StoreID,
		"name":                      sc.Name,
		"online":                    online,
		"last_heartbeat":            lastHB,
		"seconds_since_heartbeat":   secondsSince,
		"offline_threshold_seconds": int64(s.cfg.ScreenOfflineAfter.Seconds()),
	}
}

func generateToken() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return "sbscr_" + hex.EncodeToString(b), nil
}
