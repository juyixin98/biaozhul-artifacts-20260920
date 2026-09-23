// Package httpapi exposes the TWAP service over HTTP using go-chi.
package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"twap-service/internal/auth"
	"twap-service/internal/domain"
	"twap-service/internal/service"
	"twap-service/internal/store"
)

type API struct {
	svc       *service.Service
	st        *store.Store
	adminKey  string
	maxSkewUs int64
}

func New(svc *service.Service, st *store.Store, adminKey string, maxSkew time.Duration) *API {
	return &API{
		svc:       svc,
		st:        st,
		adminKey:  adminKey,
		maxSkewUs: int64(maxSkew / time.Microsecond),
	}
}

func (a *API) Router() http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.RealIP)
	r.Use(middleware.Recoverer)
	r.Use(middleware.Timeout(30 * time.Second))

	r.Get("/healthz", a.health)
	r.Get("/readyz", a.ready)

	r.Route("/v1", func(r chi.Router) {
		r.Post("/admin/sources", a.basicAdmin(a.createSource))
		r.Post("/admin/rebuild", a.basicAdmin(a.fullRebuild))
		r.Post("/samples", a.ingest)
		r.Get("/symbols/{symbol}/windows/{start}", a.getWindow)
		r.Get("/symbols/{symbol}/windows", a.listWindows)
	})
	return r
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]any{"error": code, "message": msg})
}

func (a *API) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (a *API) ready(w http.ResponseWriter, r *http.Request) {
	if err := a.st.Pool().Ping(r.Context()); err != nil {
		writeErr(w, http.StatusServiceUnavailable, "not_ready", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
}

// ---- admin auth (HTTP Basic: username "admin", password admin key) ----

func (a *API) basicAdmin(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		u, p, ok := r.BasicAuth()
		if !ok || !constantEqual(u, "admin") || !constantEqual(p, a.adminKey) {
			w.Header().Set("WWW-Authenticate", `Basic realm="twap-admin"`)
			writeErr(w, http.StatusUnauthorized, "unauthorized", "admin credentials required")
			return
		}
		h(w, r)
	}
}

type sourceReq struct {
	Name     string `json:"name"`
	Priority int    `json:"priority"`
	Secret   string `json:"secret,omitempty"` // optional; server generates when empty
}

func (a *API) createSource(w http.ResponseWriter, r *http.Request) {
	var req sourceReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "bad_json", err.Error())
		return
	}
	req.Name = strings.TrimSpace(req.Name)
	if req.Name == "" {
		writeErr(w, http.StatusBadRequest, "invalid", "name required")
		return
	}
	secret := req.Secret
	if secret == "" {
		s, err := auth.GenerateSecret()
		if err != nil {
			writeErr(w, http.StatusInternalServerError, "crypto", err.Error())
			return
		}
		secret = s
	}
	if err := a.svc.RegisterSource(r.Context(), req.Name, req.Priority, secret); err != nil {
		writeErr(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"name": req.Name, "priority": req.Priority,
		"secret_base64": secret,
		"signing":       "base64(HMAC_SHA256(secret, ts_us+'\\n'+nonce+'\\n'+body))",
	})
}

func (a *API) fullRebuild(w http.ResponseWriter, r *http.Request) {
	counts, err := a.svc.FullRebuild(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "rebuild_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"materialized_windows": counts})
}

// ---- ingest ----

type sampleReq struct {
	Symbol string `json:"symbol"`
	TSUsec int64  `json:"ts_us"`
	Price  int64  `json:"price"`
}

type batchReq struct {
	Samples []sampleReq `json:"samples"`
}

func (a *API) ingest(w http.ResponseWriter, r *http.Request) {
	source := r.Header.Get("X-Source")
	tsHdr := r.Header.Get("X-Timestamp-Usec")
	nonce := r.Header.Get("X-Nonce")
	sig := r.Header.Get("X-Signature")
	if source == "" || tsHdr == "" || nonce == "" || sig == "" {
		writeErr(w, http.StatusUnauthorized, "missing_auth_headers",
			"X-Source, X-Timestamp-Usec, X-Nonce and X-Signature are required")
		return
	}
	tsUs, err := strconv.ParseInt(tsHdr, 10, 64)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_timestamp", "X-Timestamp-Usec must be microsecond epoch")
		return
	}
	rec, err := a.st.GetSource(r.Context(), source)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeErr(w, http.StatusUnauthorized, "unknown_source", "source is not registered")
			return
		}
		writeErr(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	body, err := readBody(r)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_body", err.Error())
		return
	}
	ok, err := auth.Verify(rec.SecretKey, tsHdr, nonce, sig, body)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_signature_encoding", err.Error())
		return
	}
	if !ok {
		writeErr(w, http.StatusUnauthorized, "bad_signature", "HMAC verification failed")
		return
	}
	nowUs := time.Now().UnixMicro()
	if tsUs < nowUs-a.maxSkewUs || tsUs > nowUs+a.maxSkewUs {
		writeErr(w, http.StatusUnauthorized, "timestamp_skew",
			"request timestamp outside the allowed skew window")
		return
	}

	// Parse payload: single object or {"samples":[...]}.
	var single sampleReq
	var batch batchReq
	trimmed := strings.TrimSpace(string(body))
	if strings.HasPrefix(trimmed, "{") && strings.Contains(trimmed, "\"samples\"") {
		if err := json.Unmarshal(body, &batch); err != nil {
			writeErr(w, http.StatusBadRequest, "bad_json", err.Error())
			return
		}
	} else {
		if err := json.Unmarshal(body, &single); err != nil {
			writeErr(w, http.StatusBadRequest, "bad_json", err.Error())
			return
		}
		batch.Samples = []sampleReq{single}
	}
	if len(batch.Samples) == 0 {
		writeErr(w, http.StatusBadRequest, "empty_batch", "at least one sample required")
		return
	}
	for _, s := range batch.Samples {
		if s.Symbol == "" {
			writeErr(w, http.StatusBadRequest, "invalid", "symbol required")
			return
		}
	}

	// Single-use nonce (real replay protection) inside the first write tx of
	// the batch. We consume it up front so a replayed batch never ingests.
	tx, err := a.st.Pool().Begin(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	fresh, err := a.st.ConsumeNonce(r.Context(), tx, source, nonce, tsUs, a.maxSkewUs)
	if err != nil {
		tx.Rollback(r.Context())
		writeErr(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	if !fresh {
		tx.Rollback(r.Context())
		writeErr(w, http.StatusConflict, "nonce_replayed", "this nonce was already used")
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, http.StatusInternalServerError, "db", err.Error())
		return
	}

	results := make([]*service.IngestResult, 0, len(batch.Samples))
	status := http.StatusOK
	for _, s := range batch.Samples {
		res, ierr := a.svc.IngestSample(r.Context(), domain.Sample{
			Symbol: s.Symbol, TS: s.TSUsec, Price: s.Price, Source: source,
		})
		if ierr != nil {
			var ce *service.ConflictError
			var le *service.LateError
			switch {
			case errors.As(ierr, &ce):
				writeJSON(w, http.StatusConflict, map[string]any{
					"error":   "source_conflict",
					"message": ierr.Error(),
					"detail":  ce.Info,
				})
				return
			case errors.As(ierr, &le):
				writeJSON(w, http.StatusUnprocessableEntity, map[string]any{
					"error": "late_data_rejected", "message": ierr.Error(), "detail": le,
				})
				return
			default:
				writeErr(w, http.StatusInternalServerError, "ingest_failed", ierr.Error())
				return
			}
		}
		if res.Outcome == service.OutcomeInserted {
			status = http.StatusCreated
		}
		results = append(results, res)
	}
	if len(results) == 1 {
		writeJSON(w, status, results[0])
		return
	}
	writeJSON(w, status, map[string]any{"accepted": results})
}

// ---- reads ----

func (a *API) getWindow(w http.ResponseWriter, r *http.Request) {
	symbol, err := url.PathUnescape(chi.URLParam(r, "symbol"))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_symbol", err.Error())
		return
	}
	start, err := strconv.ParseInt(chi.URLParam(r, "start"), 10, 64)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_start", "start must be unix seconds")
		return
	}
	cfg := a.svc.Config()
	if aligned := domain.AlignStart(start, cfg.WindowSec); aligned != start {
		writeErr(w, http.StatusBadRequest, "not_aligned",
			"start must be aligned to the "+strconv.FormatInt(cfg.WindowSec, 10)+"s grid")
		return
	}
	persist := r.URL.Query().Get("persist") != "0"
	view, err := a.svc.GetWindow(r.Context(), symbol, start, persist)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "compute_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (a *API) listWindows(w http.ResponseWriter, r *http.Request) {
	symbol, err := url.PathUnescape(chi.URLParam(r, "symbol"))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_symbol", err.Error())
		return
	}
	q := r.URL.Query()
	from, err := strconv.ParseInt(q.Get("from"), 10, 64)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_from", "from must be unix seconds")
		return
	}
	to := from + 3600 // default one hour
	if q.Get("to") != "" {
		to, err = strconv.ParseInt(q.Get("to"), 10, 64)
		if err != nil {
			writeErr(w, http.StatusBadRequest, "bad_to", "to must be unix seconds")
			return
		}
	}
	if to <= from {
		writeErr(w, http.StatusBadRequest, "bad_range", "to must be greater than from")
		return
	}
	views, err := a.svc.ListWindows(r.Context(), symbol, from, to)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"symbol": symbol, "windows": views})
}
