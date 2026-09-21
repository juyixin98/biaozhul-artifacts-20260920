package user

import (
	"errors"

	sq "github.com/Masterminds/squirrel"
	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"
	"github.com/lib/pq"

	"github.com/synapticgo/synapticgo/internal/auth"
	"github.com/synapticgo/synapticgo/internal/httpx"
)

// Service manages user accounts and API tokens.
type Service struct {
	DB *sqlx.DB
}

type registerRequest struct {
	Name string `json:"name"`
}

// RegisterResponse returns the freshly created account together with its
// raw token. The token is shown exactly once and cannot be retrieved later.
type RegisterResponse struct {
	ID        int64  `json:"id"`
	Name      string `json:"name"`
	Token     string `json:"token"`
	TokenHint string `json:"token_hint"`
}

// Register creates a user, returning the one-time bearer token.
func (s *Service) Register(c echo.Context) error {
	var req registerRequest
	if err := c.Bind(&req); err != nil {
		return httpx.ErrBadRequest("invalid JSON body")
	}
	if len(req.Name) < 1 || len(req.Name) > 128 {
		return httpx.ErrBadRequest("name must be 1..128 characters")
	}
	token, err := auth.GenerateToken()
	if err != nil {
		return httpx.ErrInternal("could not generate token")
	}

	q, args, err := sq.StatementBuilder.PlaceholderFormat(sq.Dollar).
		Insert("users").Columns("name", "token_hash").
		Values(req.Name, auth.HashToken(token)).
		Suffix("RETURNING id").ToSql()
	if err != nil {
		return err
	}
	var id int64
	if err := s.DB.GetContext(c.Request().Context(), &id, q, args...); err != nil {
		var pqErr *pq.Error
		if errors.As(err, &pqErr) && pqErr.Code == "23505" {
			return httpx.ErrConflict("user name already exists")
		}
		return err
	}
	hint := token
	if len(hint) > 12 {
		hint = hint[:12] + "..."
	}
	return c.JSON(201, RegisterResponse{
		ID:        id,
		Name:      req.Name,
		Token:     token,
		TokenHint: hint,
	})
}

// Me returns the authenticated user's profile.
func (s *Service) Me(c echo.Context) error {
	return c.JSON(200, httpx.CurrentUser(c))
}
