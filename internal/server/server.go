// Package server implements the SignalBoard HTTP API.
package server

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"signalboard/internal/db"
)

// MaxBatchImportItems is the hard limit for a single batch import.
const MaxBatchImportItems = 500

type Server struct {
	pool         *pgxpool.Pool
	q            *db.Queries
	adminToken   string
	now          func() time.Time
	offlineAfter time.Duration
}

func New(pool *pgxpool.Pool, adminToken string) *Server {
	return &Server{
		pool:         pool,
		q:            db.New(pool),
		adminToken:   adminToken,
		now:          time.Now,
		offlineAfter: 90 * time.Second,
	}
}

// Handler builds the HTTP router.
func (s *Server) Handler() http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.Recoverer)
	r.Use(middleware.RequestID)

	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	r.Route("/v1", func(r chi.Router) {
		// Screen-facing endpoints use per-screen bearer tokens.
		r.Group(func(r chi.Router) {
			r.Use(s.screenAuth)
			r.Get("/screen/menu", s.getScreenMenu)
			r.Post("/screen/heartbeat", s.postHeartbeat)
		})

		// Admin endpoints.
		r.Group(func(r chi.Router) {
			r.Use(s.adminAuth)
			r.Post("/stores", s.createStore)
			r.Get("/stores", s.listStores)
			r.Route("/stores/{storeID}", func(r chi.Router) {
				r.Get("/", s.getStore)
				r.Post("/items", s.createItem)
				r.Get("/items", s.listDraftItems)
				r.Post("/items/batch-import", s.batchImportItems)
				r.Put("/items/{itemID}", s.updateItem)
				r.Delete("/items/{itemID}", s.deleteItem)
				r.Post("/publish", s.publish)
				r.Get("/versions", s.listVersions)
				r.Get("/versions/{version}", s.getVersion)
				r.Post("/temp-prices", s.createTempPrice)
				r.Get("/temp-prices", s.listTempPrices)
				r.Delete("/temp-prices/{tempPriceID}", s.deleteTempPrice)
				r.Post("/sales-events", s.createSalesEvent)
				r.Get("/sales/daily", s.getDailySales)
				r.Post("/screens", s.createScreen)
				r.Get("/screens", s.listScreens)
			})
		})
	})
	return r
}

// --- middleware ---------------------------------------------------------------

type ctxKey string

const ctxScreen ctxKey = "screen"

func (s *Server) adminAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := bearerToken(r)
		if token == "" || subtle.ConstantTimeCompare([]byte(token), []byte(s.adminToken)) != 1 {
			writeErr(w, http.StatusUnauthorized, "unauthorized", "valid admin bearer token required")
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (s *Server) screenAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := bearerToken(r)
		if token == "" {
			writeErr(w, http.StatusUnauthorized, "unauthorized", "screen bearer token required")
			return
		}
		screen, err := s.q.GetScreenByTokenHash(r.Context(), hashToken(token))
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				writeErr(w, http.StatusUnauthorized, "unauthorized", "unknown screen token")
				return
			}
			writeErr(w, http.StatusInternalServerError, "internal", err.Error())
			return
		}
		ctx := context.WithValue(r.Context(), ctxScreen, screen)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func bearerToken(r *http.Request) string {
	h := r.Header.Get("Authorization")
	if !strings.HasPrefix(h, "Bearer ") {
		return ""
	}
	return strings.TrimSpace(strings.TrimPrefix(h, "Bearer "))
}

func hashToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// --- helpers ------------------------------------------------------------------

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]any{
		"error": map[string]string{"code": code, "message": msg},
	})
}

func decodeJSON(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeErr(w, http.StatusBadRequest, "bad_request", "invalid JSON body: "+err.Error())
		return false
	}
	return true
}

func pathUUID(w http.ResponseWriter, r *http.Request, name string) (uuid.UUID, bool) {
	id, err := uuid.Parse(chi.URLParam(r, name))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "bad_request", "invalid "+name)
		return uuid.Nil, false
	}
	return id, true
}

// noRows reports whether err is a pgx "no rows" error.
func noRows(err error) bool { return errors.Is(err, pgx.ErrNoRows) }

// withTx runs fn inside a transaction, committing on nil error.
func (s *Server) withTx(ctx context.Context, iso pgx.TxIsoLevel, fn func(q *db.Queries) error) error {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: iso})
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if err := fn(s.q.WithTx(tx)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
