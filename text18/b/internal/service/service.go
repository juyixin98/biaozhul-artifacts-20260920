package service

import (
	"context"

	"github.com/jackc/pgx/v5/pgxpool"

	"sircc/internal/config"
	"sircc/internal/db"
	"sircc/internal/middleware"
)

// Service holds the pool and generated query layer and backs every handler.
type Service struct {
	pool *pgxpool.Pool
	q    *db.Queries
	cfg  config.Config
}

func New(pool *pgxpool.Pool, cfg config.Config) *Service {
	return &Service{pool: pool, q: db.New(pool), cfg: cfg}
}

func (s *Service) Pool() *pgxpool.Pool   { return s.pool }
func (s *Service) Q() *db.Queries        { return s.q }
func (s *Service) Config() config.Config { return s.cfg }

func actorFrom(ctx context.Context) (middleware.Actor, bool) {
	return middleware.FromContext(ctx)
}

// errResult builds a result carrying a JSON error envelope; it is used inside
// idemExec so business errors are persisted as the idempotent response.
func errResult(status int, code, msg string) result {
	return result{status: status, body: map[string]any{
		"error": struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		}{Code: code, Message: msg},
	}}
}

func okResult(v any) result {
	return result{status: 200, body: v}
}
