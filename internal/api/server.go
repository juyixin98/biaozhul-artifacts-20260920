// Package api implements the HTTP API: authentication, role-based access
// control, project/asset/composition/version/job endpoints and the version
// freeze service.
package api

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/vfxqueue/renderq/internal/db/dbgen"
	"github.com/vfxqueue/renderq/internal/storage"
)

type Server struct {
	pool  *pgxpool.Pool
	q     *dbgen.Queries
	store *storage.Store
}

func NewServer(pool *pgxpool.Pool, store *storage.Store) *Server {
	return &Server{pool: pool, q: dbgen.New(pool), store: store}
}

func (s *Server) Router() http.Handler {
	r := chi.NewRouter()
	r.Use(requestID)
	r.Use(recoverer)

	r.Get("/healthz", s.health)

	r.Group(func(r chi.Router) {
		r.Use(s.auth)
		// Projects
		r.Post("/projects", s.createProject)
		r.Get("/projects", s.listProjects)
		r.Get("/projects/{projectID}", s.getProject)
		r.Post("/projects/{projectID}/members", s.addProjectMember)
		r.Get("/projects/{projectID}/members", s.listProjectMembers)

		// Assets
		r.Post("/projects/{projectID}/assets", s.uploadAsset)
		r.Get("/projects/{projectID}/assets", s.listAssets)

		// Compositions & versions
		r.Post("/projects/{projectID}/compositions", s.createComposition)
		r.Get("/projects/{projectID}/compositions", s.listCompositions)
		r.Get("/compositions/{compID}", s.getComposition)
		r.Post("/compositions/{compID}/versions", s.freezeVersion)
		r.Get("/compositions/{compID}/versions", s.listVersions)
		r.Get("/versions/{versionID}", s.getVersion)

		// Jobs
		r.Post("/versions/{versionID}/jobs", s.createJob)
		r.Get("/jobs/{jobID}", s.getJob)
		r.Get("/jobs/{jobID}/frames", s.listJobFrames)
		r.Post("/jobs/{jobID}/cancel", s.cancelJob)
		r.Get("/jobs/{jobID}/frames/{frameNo}/png", s.getFramePNG)
		r.Get("/jobs/{jobID}/summary", s.getJobSummary)
	})
	return r
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// hashKey returns the stored digest form of an API key.
func hashKey(key string) string {
	sum := sha256.Sum256([]byte(strings.TrimSpace(key)))
	return hex.EncodeToString(sum[:])
}

// HashAPIKey is the exported form used by the server bootstrap.
func HashAPIKey(key string) string { return hashKey(key) }

// failNotFound is a small helper for the common ErrNoRows -> 404 mapping.
func isNoRows(err error) bool { return err == pgx.ErrNoRows }
