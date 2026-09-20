package api

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"signalboard/internal/config"
	"signalboard/internal/db"
	"signalboard/internal/httpx"
)

type ctxKey int

const (
	ctxStoreID ctxKey = iota
	ctxScreen
)

// Server wires the connection pool to the HTTP handlers.
type Server struct {
	pool *pgxpool.Pool
	q    *db.Queries
	cfg  config.Config
}

func NewServer(pool *pgxpool.Pool, cfg config.Config) *Server {
	return &Server{pool: pool, q: db.New(pool), cfg: cfg}
}

// Router builds the application router.
func (s *Server) Router() http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.RealIP)
	r.Use(middleware.Logger)
	r.Use(middleware.Recoverer)
	r.Use(middleware.Timeout(30 * time.Second))

	r.Get("/healthz", s.healthz)

	r.Route("/v1", func(r chi.Router) {
		// Screen-facing endpoints: authenticated with a per-screen token and
		// scoped to that token's store. Screens can never name another store.
		r.Group(func(r chi.Router) {
			r.Use(s.screenAuth)
			r.Get("/screen/menu", s.getScreenMenu)
			r.Post("/screen/heartbeat", s.screenHeartbeat)
			r.Get("/screen/status", s.screenStatus)
		})

		// Management/ingest endpoints.
		r.Group(func(r chi.Router) {
			r.Use(s.adminAuth)

			r.Post("/admin/stores", s.createStore)
			r.Get("/admin/stores", s.listStores)
			r.Get("/admin/stores/{storeID}", s.getStore)

			r.Post("/admin/stores/{storeID}/dishes", s.createDish)
			r.Get("/admin/stores/{storeID}/dishes", s.listDishes)
			r.Put("/admin/stores/{storeID}/dishes/{dishID}", s.updateDish)
			r.Delete("/admin/stores/{storeID}/dishes/{dishID}", s.deleteDish)
			r.Post("/admin/stores/{storeID}/dishes/import", s.importDishes)

			r.Post("/admin/stores/{storeID}/publish", s.publishMenu)
			r.Get("/admin/stores/{storeID}/versions", s.listVersions)
			r.Get("/admin/stores/{storeID}/versions/{version}", s.getVersion)

			r.Post("/admin/stores/{storeID}/sales-events", s.ingestSalesEvent)
			r.Get("/admin/stores/{storeID}/daily-sales", s.getDailySales)

			r.Post("/admin/stores/{storeID}/screens", s.createScreen)
			r.Get("/admin/stores/{storeID}/screens", s.listScreens)
		})
	})

	return r
}

func (s *Server) healthz(w http.ResponseWriter, r *http.Request) {
	if err := s.pool.Ping(r.Context()); err != nil {
		httpx.ErrorJSON(w, http.StatusServiceUnavailable, "db_unavailable", err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"status":"ok"}`))
}

// adminAuth enforces Authorization: Bearer <ADMIN_TOKEN>.
func (s *Server) adminAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tok, ok := bearerToken(r)
		if !ok || subtle.ConstantTimeCompare([]byte(tok), []byte(s.cfg.AdminToken)) != 1 {
			w.Header().Set("WWW-Authenticate", `Bearer realm="admin"`)
			httpx.ErrorJSON(w, http.StatusUnauthorized, "unauthorized", "missing or invalid admin token")
			return
		}
		next.ServeHTTP(w, r)
	})
}

// screenAuth resolves the X-Screen-Token (or "Authorization: Screen <tok>")
// to a screen row and injects both the screen and its (single) store id.
func (s *Server) screenAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := strings.TrimSpace(r.Header.Get("X-Screen-Token"))
		if token == "" {
			if scheme, val, ok := strings.Cut(r.Header.Get("Authorization"), " "); ok &&
				strings.EqualFold(strings.TrimSpace(scheme), "Screen") {
				token = strings.TrimSpace(val)
			}
		}
		if token == "" {
			httpx.ErrorJSON(w, http.StatusUnauthorized, "unauthorized", "missing screen token")
			return
		}
		screen, err := s.q.GetScreenByTokenHash(r.Context(), hashToken(token))
		if err != nil {
			httpx.ErrorJSON(w, http.StatusUnauthorized, "unauthorized", "invalid screen token")
			return
		}
		ctx := context.WithValue(r.Context(), ctxScreen, screen)
		ctx = context.WithValue(ctx, ctxStoreID, screen.StoreID)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func bearerToken(r *http.Request) (string, bool) {
	scheme, val, ok := strings.Cut(r.Header.Get("Authorization"), " ")
	if !ok || !strings.EqualFold(strings.TrimSpace(scheme), "Bearer") {
		return "", false
	}
	val = strings.TrimSpace(val)
	return val, val != ""
}

func screenFromCtx(ctx context.Context) db.Screen {
	return ctx.Value(ctxScreen).(db.Screen)
}

// hashToken returns the hex SHA-256 digest of an opaque screen token. Only the
// digest is ever persisted.
func hashToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// snapshot runs fn inside one read transaction with a single consistency
// snapshot, so every response reflects one immutable instant with no mixing of
// old and new state.
func (s *Server) snapshot(ctx context.Context, fn func(q *db.Queries) error) error {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if err := fn(s.q.WithTx(tx)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func init() {
	// Keep chi's request logger on stdout in the container's log format.
	log.SetFlags(log.LstdFlags | log.Lmsgprefix)
}
