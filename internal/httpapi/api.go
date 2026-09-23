// Package httpapi wires the engine to a small JSON HTTP API.
package httpapi

import (
	"bytes"
	"crypto/subtle"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"time"

	"sensorhealth/internal/clock"
	"sensorhealth/internal/cryptox"
	"sensorhealth/internal/engine"
	"sensorhealth/internal/model"
	"sensorhealth/internal/store"
)

// Config holds API construction parameters.
type Config struct {
	IngestSecret string
	AdminToken   string
	ReplayWindow time.Duration
}

// Handler exposes the service over HTTP.
type Handler struct {
	eng  *engine.Engine
	st   *store.Store
	clk  clock.Clock
	vclk *clock.Virtual // nil when running on the real clock
	cfg  Config
	mux  *http.ServeMux
}

// NewHandler builds the router. vclk may be nil when the server runs on
// wall-clock time; in that mode the clock-advance endpoint reports 400.
func NewHandler(eng *engine.Engine, st *store.Store, clk clock.Clock, vclk *clock.Virtual, cfg Config) *Handler {
	if cfg.ReplayWindow == 0 {
		cfg.ReplayWindow = 5 * time.Minute
	}
	h := &Handler{eng: eng, st: st, clk: clk, vclk: vclk, cfg: cfg, mux: http.NewServeMux()}
	h.routes()
	return h
}

// ServeHTTP implements http.Handler.
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	h.mux.ServeHTTP(w, r)
}

func (h *Handler) routes() {
	h.mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	// Signed ingestion endpoint.
	h.mux.HandleFunc("POST /v1/ingest", h.requireIngestSig(h.ingest))

	// Read endpoints.
	h.mux.HandleFunc("GET /v1/devices", h.listDevices)
	h.mux.HandleFunc("GET /v1/devices/{id}", h.getDevice)
	h.mux.HandleFunc("GET /v1/devices/{id}/messages", h.getMessages)
	h.mux.HandleFunc("GET /v1/events", h.listEvents)
	h.mux.HandleFunc("GET /v1/config/device-types", h.listTypes)

	// Admin endpoints (bearer token).
	h.mux.HandleFunc("GET /admin/config/device-types", h.adminAuth(h.listTypes))
	h.mux.HandleFunc("PUT /admin/config/device-types/{type}", h.adminAuth(h.upsertType))
	h.mux.HandleFunc("POST /admin/clock/advance", h.adminAuth(h.clockAdvance))
	h.mux.HandleFunc("GET /admin/clock", h.adminAuth(h.clockGet))
}

// ---------- ingestion ----------

type ingestRequest struct {
	Messages []model.Message `json:"messages"`
	// Mode is "live" (default; device's current stream — clock rollback and
	// all rules apply) or "backfill" (ordered historical replay; old
	// timestamps and low sequence numbers are expected).
	Mode string `json:"mode"`
}

func (h *Handler) ingest(w http.ResponseWriter, r *http.Request) {
	var req ingestRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if len(req.Messages) == 0 {
		writeError(w, http.StatusBadRequest, "messages must not be empty")
		return
	}
	opts := engine.IngestOptions{}
	switch req.Mode {
	case "", "live":
	case "backfill":
		opts.Backfill = true
	default:
		writeError(w, http.StatusBadRequest, `invalid mode: want "live" or "backfill"`)
		return
	}
	res, err := h.eng.Ingest(r.Context(), req.Messages, opts)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "ingest failed: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

// ---------- queries ----------

func (h *Handler) listDevices(w http.ResponseWriter, r *http.Request) {
	all, err := h.eng.AllHealth(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if all == nil {
		all = []model.Health{}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"server_time": h.clk.Now().UTC(),
		"devices":     all,
	})
}

func (h *Handler) getDevice(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	hh, err := h.eng.Health(r.Context(), id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if hh == nil {
		writeError(w, http.StatusNotFound, "device not found: "+id)
		return
	}
	writeJSON(w, http.StatusOK, hh)
}

func (h *Handler) getMessages(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	msgs, err := h.st.ListMessages(r.Context(), id, limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if msgs == nil {
		msgs = []model.StoredMessage{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"device_id": id, "messages": msgs})
}

func (h *Handler) listEvents(w http.ResponseWriter, r *http.Request) {
	f := store.EventFilter{
		DeviceID: r.URL.Query().Get("device_id"),
		Rule:     r.URL.Query().Get("rule"),
	}
	switch r.URL.Query().Get("open") {
	case "true":
		b := true
		f.Open = &b
	case "false":
		b := false
		f.Open = &b
	}
	f.Limit, _ = strconv.Atoi(r.URL.Query().Get("limit"))
	evs, err := h.st.ListEvents(r.Context(), f)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if evs == nil {
		evs = []model.Event{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": evs})
}

func (h *Handler) listTypes(w http.ResponseWriter, r *http.Request) {
	types, err := h.st.ListDeviceTypes(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	gv, _ := h.st.GlobalConfigVersion(r.Context())
	writeJSON(w, http.StatusOK, map[string]any{
		"global_version": gv,
		"device_types":   types,
	})
}

// ---------- admin ----------

func (h *Handler) upsertType(w http.ResponseWriter, r *http.Request) {
	typ := r.PathValue("type")
	var d model.DeviceType
	if err := json.NewDecoder(r.Body).Decode(&d); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	d.Type = typ
	saved, err := h.st.UpsertDeviceType(r.Context(), d)
	if err != nil {
		// DeviceType.Validate errors (and the empty-type guard) are 400s.
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, saved)
}

func (h *Handler) clockGet(w http.ResponseWriter, _ *http.Request) {
	mode := "virtual"
	if h.vclk == nil {
		mode = "real"
	}
	writeJSON(w, http.StatusOK, map[string]string{
		"now":  h.clk.Now().UTC().Format(time.RFC3339Nano),
		"mode": mode,
	})
}

type clockAdvanceReq struct {
	AdvanceMs int64  `json:"advance_ms"`
	SetTo     string `json:"set_to"`
	Sweep     *bool  `json:"sweep"`
}

func (h *Handler) clockAdvance(w http.ResponseWriter, r *http.Request) {
	if h.vclk == nil {
		writeError(w, http.StatusBadRequest,
			"server runs on the real clock; restart with --clock=virtual to advance time")
		return
	}
	var req clockAdvanceReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if req.SetTo != "" {
		t, err := time.Parse(time.RFC3339, req.SetTo)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid set_to: want RFC3339: "+err.Error())
			return
		}
		h.vclk.Set(t.UTC())
	}
	if req.AdvanceMs > 0 {
		h.vclk.Advance(time.Duration(req.AdvanceMs) * time.Millisecond)
	}
	sweep := true
	if req.Sweep != nil {
		sweep = *req.Sweep
	}
	fired := 0
	if sweep {
		n, err := h.eng.Sweep(r.Context())
		if err != nil {
			writeError(w, http.StatusInternalServerError, "sweep failed: "+err.Error())
			return
		}
		fired = n
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"now":                h.vclk.Now().UTC(),
		"stale_alerts_fired": fired,
	})
}

// ---------- auth middleware ----------

func (h *Handler) adminAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		want := "Bearer " + h.cfg.AdminToken
		got := r.Header.Get("Authorization")
		if h.cfg.AdminToken == "" || subtle.ConstantTimeCompare([]byte(want), []byte(got)) != 1 {
			writeError(w, http.StatusUnauthorized, "invalid or missing admin token")
			return
		}
		next(w, r)
	}
}

func (h *Handler) requireIngestSig(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		raw, err := readBody(r)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		sig := r.Header.Get("X-Signature")
		tsStr := r.Header.Get("X-Timestamp")
		if sig == "" || tsStr == "" {
			writeError(w, http.StatusUnauthorized, "missing X-Signature or X-Timestamp headers")
			return
		}
		ts, err := time.Parse(time.RFC3339, tsStr)
		if err != nil {
			writeError(w, http.StatusUnauthorized, "invalid X-Timestamp (want RFC3339)")
			return
		}
		if err := cryptox.VerifyFresh(h.cfg.IngestSecret, trimScheme(sig),
			ts.UTC(), raw, h.clk.Now().UTC(), h.cfg.ReplayWindow); err != nil {
			writeError(w, http.StatusUnauthorized, err.Error())
			return
		}
		r.Body = newBytesBody(raw)
		r.ContentLength = int64(len(raw))
		next(w, r)
	}
}

func trimScheme(sig string) string {
	const p = "sha256="
	if len(sig) > len(p) && sig[:len(p)] == p {
		return sig[len(p):]
	}
	return sig
}

// ---------- helpers ----------

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func readBody(r *http.Request) ([]byte, error) {
	defer r.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(r.Body, 8<<20))
	if err != nil {
		return nil, err
	}
	return raw, nil
}

func newBytesBody(b []byte) io.ReadCloser {
	return io.NopCloser(bytes.NewReader(b))
}
