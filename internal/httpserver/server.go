package httpserver

import (
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"communityvault/internal/content"
	"communityvault/internal/moderation"
	"communityvault/internal/rules"
	"communityvault/internal/view"
)

type Server struct {
	pool  *pgxpool.Pool
	cts   *content.Service
	mod   *moderation.Service
	rules *rules.Service
	views *view.Service
}

func NewServer(pool *pgxpool.Pool, cts *content.Service, mod *moderation.Service, rs *rules.Service, vs *view.Service) *Server {
	return &Server{pool: pool, cts: cts, mod: mod, rules: rs, views: vs}
}

func (s *Server) Router() http.Handler {
	r := chi.NewRouter()
	r.Use(Auth(s.pool))

	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, 200, map[string]string{"status": "ok", "time": time.Now().UTC().Format(time.RFC3339)})
	})

	// Public stable-cursor feed.
	r.Get("/v1/feed", s.handleFeed)
	// Categories (public).
	r.Get("/v1/categories", s.handleListCategories)

	r.Route("/v1/contents", func(r chi.Router) {
		r.Post("/", s.handleCreateContent)
		r.Get("/{id}", s.handleGetContent)
		r.Get("/{id}/revisions", s.handleListRevisions)
		r.Put("/{id}", s.handleEditContent)
		r.Post("/{id}/submit", s.handleSubmit)
		r.Post("/{id}/withdraw", s.handleWithdraw)
		r.Post("/{id}/rollback", s.handleRollback)
		r.Post("/{id}/reports", s.handleReport)
		r.Get("/{id}/events", s.handleEvents)
		r.Get("/{id}/reviews", s.handleReviews)
		r.Get("/{id}/reports", s.handleListReports)
	})

	r.Route("/v1/moderation", func(r chi.Router) {
		r.Post("/claims", s.handleClaim)
		r.Post("/claims/{taskID}/approve", s.handleApprove)
		r.Post("/claims/{taskID}/reject", s.handleReject)
		r.Post("/recycle", s.handleRecycle)
		r.Get("/reports", s.handleAllReports)
	})

	r.Route("/v1/rules", func(r chi.Router) {
		r.Get("/", s.handleListRules)
		r.Get("/active", s.handleActiveRule)
		r.Post("/", s.handleCreateRule)
		r.Post("/{id}/activate", s.handleActivateRule)
	})

	return r
}
