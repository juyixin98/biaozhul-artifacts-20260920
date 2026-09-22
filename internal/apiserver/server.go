// Package apiserver exposes the VFX queue HTTP API.
package apiserver

import (
	"encoding/json"
	"log"
	"net/http"

	"vfxqueue/internal/auth"
	"vfxqueue/internal/config"
	"vfxqueue/internal/db/gen"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Server struct {
	pool *pgxpool.Pool
	q    *gen.Queries
	cfg  config.Config
	logf func(string, ...any)
}

func New(pool *pgxpool.Pool, q *gen.Queries, cfg config.Config) *Server {
	return &Server{pool: pool, q: q, cfg: cfg, logf: log.Printf}
}

// Router builds the fully wired HTTP handler.
func (s *Server) Router() http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.RealIP)
	r.Use(middleware.Recoverer)

	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	r.Group(func(r chi.Router) {
		r.Use(auth.Middleware(s.pool, s.q))

		r.Get("/v1/me", s.getMe)
		r.Post("/v1/projects", s.createProject)
		r.Get("/v1/projects", s.listProjects)

		r.Route("/v1/projects/{projectID}", func(r chi.Router) {
			r.Use(s.projectContext)
			r.Get("/", s.getProject)

			r.Post("/members", s.addMember)
			r.Get("/members", s.listMembers)

			r.Post("/assets", s.uploadAsset)
			r.Get("/assets", s.listAssets)

			r.Post("/compositions", s.createComposition)
			r.Get("/compositions", s.listCompositions)

			r.Post("/tasks", s.enqueueTask)
			r.Get("/tasks", s.listTasks)
		})

		r.Get("/v1/tasks/{taskID}", s.getTask)
		r.Post("/v1/tasks/{taskID}/cancel", s.cancelTask)
		r.Get("/v1/tasks/{taskID}/frames", s.listTaskFrames)
		r.Get("/v1/tasks/{taskID}/frames/{frameIndex}", s.getFrame)
		r.Get("/v1/tasks/{taskID}/frames/{frameIndex}/png", s.framePNG)
	})

	return r
}

// ---- helpers ----

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func decodeJSON(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	return dec.Decode(v)
}
