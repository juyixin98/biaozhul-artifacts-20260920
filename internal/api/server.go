package api

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"

	"sensorhealth/internal/crypto"
	"sensorhealth/internal/domain"
	"sensorhealth/internal/service"
)

// Server wires the engine to HTTP.
type Server struct {
	svc    *service.Service
	signer *crypto.Signer
	admin  string // bearer token for admin endpoints
}

func NewServer(svc *service.Service, signer *crypto.Signer, adminToken string) *Server {
	return &Server{svc: svc, signer: signer, admin: adminToken}
}

func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/v1/ingest", s.handleIngest)

	mux.HandleFunc("GET /api/v1/devices/{id}/health", s.handleHealth)
	mux.HandleFunc("GET /api/v1/alerts", s.handleListAlerts)

	mux.HandleFunc("POST /admin/devices", s.adminAuth(s.handleRegister))
	mux.HandleFunc("GET /admin/device-types/{type}/config", s.adminAuth(s.handleGetConfig))
	mux.HandleFunc("PUT /admin/device-types/{type}/config", s.adminAuth(s.handlePutConfig))
	mux.HandleFunc("POST /admin/sweep", s.adminAuth(s.handleSweep))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	return logging(mux)
}

// --- ingestion (HMAC authenticated) ----------------------------------------

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	raw, err := io.ReadAll(io.LimitReader(r.Body, 4<<20))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "read body: "+err.Error())
		return
	}
	defer r.Body.Close()

	timestamp, err := time.Parse(time.RFC3339Nano, r.Header.Get("X-Timestamp"))
	if err != nil {
		// also accept unix milli
		var ms int64
		if jsonErr := json.Unmarshal([]byte(r.Header.Get("X-Timestamp")), &ms); jsonErr == nil && ms > 0 {
			timestamp = time.UnixMilli(ms)
		} else {
			writeErr(w, http.StatusUnauthorized, "missing/invalid X-Timestamp")
			return
		}
	}
	nonce := r.Header.Get("X-Nonce")
	sig := r.Header.Get("X-Signature")
	if nonce == "" || sig == "" {
		writeErr(w, http.StatusUnauthorized, "X-Nonce and X-Signature are required")
		return
	}
	if err := s.signer.Verify(http.MethodPost, "/api/v1/ingest", timestamp, nonce, sig, raw, time.Now()); err != nil {
		switch {
		case errors.Is(err, crypto.ErrBadSignature):
			writeErr(w, http.StatusUnauthorized, err.Error())
		case errors.Is(err, crypto.ErrTimestampSkew):
			writeErr(w, http.StatusUnauthorized, err.Error())
		case errors.Is(err, crypto.ErrReplay):
			writeErr(w, http.StatusConflict, err.Error())
		default:
			writeErr(w, http.StatusUnauthorized, err.Error())
		}
		return
	}

	var env Envelope
	if err := json.Unmarshal(raw, &env); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return
	}
	if env.DeviceID == "" || len(env.Messages) == 0 {
		writeErr(w, http.StatusBadRequest, "device_id and at least one message are required")
		return
	}

	msgs := make([]service.IncomingMessage, 0, len(env.Messages))
	now := time.Now()
	for _, m := range env.Messages {
		recv := now
		im := service.IncomingMessage{
			IsHeartbeat: m.Kind == "heartbeat",
			Seq:         m.Seq,
			Value:       m.Value,
			SampledAt:   m.SampledAt,
			ReceivedAt:  recv,
		}
		if m.SampledAt.IsZero() {
			writeErr(w, http.StatusBadRequest, "every message requires sampled_at")
			return
		}
		msgs = append(msgs, im)
	}

	res, err := s.svc.IngestBatch(r.Context(), env.DeviceID, msgs)
	if err != nil {
		if errors.Is(err, service.ErrUnknownDevice) {
			writeErr(w, http.StatusNotFound, err.Error())
			return
		}
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, res)
}

// --- read endpoints ---------------------------------------------------------

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	h, err := s.svc.Health(r.Context(), id)
	if err != nil {
		if errors.Is(err, service.ErrUnknownDevice) {
			writeErr(w, http.StatusNotFound, err.Error())
			return
		}
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, h)
}

func (s *Server) handleListAlerts(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	status := domain.AlertStatus(q.Get("status"))
	kind := domain.Kind(q.Get("kind"))
	limit := 100
	if l := q.Get("limit"); l != "" {
		if _, err := parseInt(&limit, l); err != nil {
			writeErr(w, http.StatusBadRequest, "invalid limit")
			return
		}
	}
	alerts, err := s.svc.ListAlerts(r.Context(), q.Get("device_id"), status, kind, limit)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	if alerts == nil {
		alerts = []domain.Alert{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"alerts": alerts})
}

// --- admin ------------------------------------------------------------------

func (s *Server) adminAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.admin == "" {
			writeErr(w, http.StatusServiceUnavailable, "admin API disabled")
			return
		}
		got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if got == "" || got != s.admin {
			writeErr(w, http.StatusUnauthorized, "admin bearer token required")
			return
		}
		next(w, r)
	}
}

func (s *Server) handleRegister(w http.ResponseWriter, r *http.Request) {
	var req registerRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	d, ver, err := s.svc.RegisterDevice(r.Context(), req.ID, req.Type)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"device": d, "config_version": ver})
}

func (s *Server) handleGetConfig(w http.ResponseWriter, r *http.Request) {
	c, err := s.svc.GetConfig(r.Context(), r.PathValue("type"))
	if err != nil {
		writeErr(w, http.StatusNotFound, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, c)
}

func (s *Server) handlePutConfig(w http.ResponseWriter, r *http.Request) {
	var req configRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	devType := r.PathValue("type")
	if req.DeviceType != "" && req.DeviceType != devType {
		writeErr(w, http.StatusBadRequest, "device_type in body and path differ")
		return
	}
	c := domain.RuleConfig{
		DeviceType:          devType,
		FrozenEnterCount:    req.FrozenEnterCount,
		FrozenRecoverCount:  req.FrozenRecoverCount,
		MissingEnterCount:   req.MissingEnterCount,
		MissingRecoverCount: req.MissingRecoverCount,
		BackfillLookback:    req.BackfillLookback,
	}
	var err error
	if c.StaleEnterTimeout, err = parseDur(req.StaleEnterTimeout); err != nil {
		writeErr(w, http.StatusBadRequest, "stale_enter_timeout: "+err.Error())
		return
	}
	if c.StaleRecoverTimeout, err = parseDur(req.StaleRecoverTimeout); err != nil {
		writeErr(w, http.StatusBadRequest, "stale_recover_timeout: "+err.Error())
		return
	}
	if c.FrozenEnterMinDuration, err = parseDur(req.FrozenEnterMinDuration); err != nil {
		writeErr(w, http.StatusBadRequest, "frozen_enter_min_duration: "+err.Error())
		return
	}
	ver, err := s.svc.UpdateConfig(r.Context(), c)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	c.Version = ver
	writeJSON(w, http.StatusOK, c)
}

func (s *Server) handleSweep(w http.ResponseWriter, r *http.Request) {
	if err := s.svc.SweepStale(r.Context(), time.Now()); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "swept"})
}

// --- helpers ----------------------------------------------------------------

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, errorBody{Error: msg})
}

func parseInt(out *int, s string) (int, error) {
	var n int
	for _, ch := range s {
		if ch < '0' || ch > '9' {
			return 0, errors.New("not an integer")
		}
		n = n*10 + int(ch-'0')
	}
	*out = n
	return n, nil
}

func parseDur(s string) (domain.Duration, error) {
	if s == "" {
		return 0, errors.New("empty duration")
	}
	d, err := time.ParseDuration(s)
	return domain.Duration(d), err
}
