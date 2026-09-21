// Package server implements the SignalBoard HTTP API.
package server

import (
	"encoding/json"
	"errors"
	"net/http"
	"regexp"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"signalboard/internal/db"
)

// maxImportItems is the batch-import limit; larger batches are rejected.
const maxImportItems = 500

var itemKeyRE = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,63}$`)

// Server holds the dependencies shared by all handlers.
type Server struct {
	q    *db.Queries
	pool *pgxpool.Pool
}

// New builds the root HTTP handler.
func New(q *db.Queries, pool *pgxpool.Pool) http.Handler {
	s := &Server{q: q, pool: pool}
	r := chi.NewRouter()
	r.Use(middleware.Recoverer)
	r.Use(middleware.RequestID)

	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })

	r.Route("/api/stores", func(r chi.Router) {
		r.Post("/", s.createStore)
		r.Route("/{storeID}", func(r chi.Router) {
			r.Get("/", s.getStore)
			r.Get("/menu", s.getLiveMenu) // admin live preview
			r.Get("/versions", s.listVersions)
			r.Post("/publish", s.publish)
			r.Route("/draft", func(r chi.Router) {
				r.Get("/", s.getDraft)
				r.Post("/import", s.importDraft)
				r.Put("/items/{itemKey}", s.upsertDraftItem)
				r.Delete("/items/{itemKey}", s.deleteDraftItem)
				r.Post("/temp-prices", s.createDraftTempPrice)
				r.Delete("/temp-prices/{tempPriceID}", s.deleteDraftTempPrice)
			})
			r.Post("/sales-events", s.recordSalesEvent)
			r.Get("/sales", s.listDailySales)
			r.Post("/screens", s.createScreen)
			r.Get("/screens", s.listScreens)
		})
	})

	// Screen-facing API, authenticated per-screen by bearer token.
	r.Group(func(r chi.Router) {
		r.Use(s.screenAuth)
		r.Get("/screen/menu", s.screenMenu)
		r.Post("/screen/heartbeat", s.heartbeat)
	})

	return r
}

// --- helpers ---------------------------------------------------------------

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func decodeJSON(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return false
	}
	return true
}

func uuidParam(w http.ResponseWriter, r *http.Request, name string) (pgtype.UUID, bool) {
	var id pgtype.UUID
	if err := id.Scan(chi.URLParam(r, name)); err != nil {
		writeError(w, http.StatusBadRequest, "invalid "+name)
		return id, false
	}
	return id, true
}

func mustUUID(s string) pgtype.UUID {
	var id pgtype.UUID
	if err := id.Scan(s); err != nil {
		panic(err)
	}
	return id
}

func uuidString(id pgtype.UUID) string {
	u, _ := uuid.FromBytes(id.Bytes[:])
	return u.String()
}

// pgErrorCode extracts a PostgreSQL error code, if any.
func pgErrorCode(err error) string {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code
	}
	return ""
}

func isNoRows(err error) bool { return errors.Is(err, pgx.ErrNoRows) }

func pathParam(r *http.Request, name string) string { return chi.URLParam(r, name) }
