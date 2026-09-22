// Package auth provides bearer-token authentication and role-based access
// control for DAMS. Tokens are local-test-only random strings; only their
// SHA-256 hashes are stored.
package auth

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Role string

const (
	RoleAdmin   Role = "admin"
	RoleAnalyst Role = "analyst"
	RoleAuditor Role = "auditor"
)

type Principal struct {
	UserID      int64
	Email       string
	DisplayName string
	// Membership roles keyed by organization id.
	Roles map[int64]Role
}

// GenerateToken returns a fresh opaque local-test token (shown once).
func GenerateToken() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return "dams_" + hex.EncodeToString(b), nil
}

func HashToken(token string) string {
	sum := sha256.Sum256([]byte(strings.TrimSpace(token)))
	return hex.EncodeToString(sum[:])
}

type principalCtxKey struct{}

// Authenticate resolves a bearer token to a principal with all of the user's
// organization roles.
func Authenticate(ctx context.Context, pool *pgxpool.Pool, token string) (Principal, error) {
	token = strings.TrimSpace(token)
	if token == "" {
		return Principal{}, ErrUnauthenticated
	}
	var p Principal
	err := pool.QueryRow(ctx, `
		SELECT u.id, u.email, u.display_name
		FROM api_tokens t JOIN users u ON u.id = t.user_id
		WHERE t.token_hash = $1`, HashToken(token),
	).Scan(&p.UserID, &p.Email, &p.DisplayName)
	if errors.Is(err, pgx.ErrNoRows) {
		return Principal{}, ErrUnauthenticated
	}
	if err != nil {
		return Principal{}, err
	}
	rows, err := pool.Query(ctx,
		`SELECT org_id, role FROM memberships WHERE user_id = $1`, p.UserID)
	if err != nil {
		return Principal{}, err
	}
	defer rows.Close()
	p.Roles = make(map[int64]Role)
	for rows.Next() {
		var orgID int64
		var role string
		if err := rows.Scan(&orgID, &role); err != nil {
			return Principal{}, err
		}
		p.Roles[orgID] = Role(role)
	}
	return p, rows.Err()
}

func WithPrincipal(ctx context.Context, p Principal) context.Context {
	return context.WithValue(ctx, principalCtxKey{}, &p)
}

func FromContext(ctx context.Context) (Principal, bool) {
	p, ok := ctx.Value(principalCtxKey{}).(*Principal)
	if !ok || p == nil {
		return Principal{}, false
	}
	return *p, true
}

// RoleIn returns the principal's role in the org and whether they are a member.
func (p Principal) RoleIn(orgID int64) (Role, bool) {
	r, ok := p.Roles[orgID]
	return r, ok
}

// HasRole reports membership with one of the allowed roles.
func (p Principal) HasRole(orgID int64, allowed ...Role) bool {
	r, ok := p.Roles[orgID]
	if !ok {
		return false
	}
	for _, a := range allowed {
		if r == a {
			return true
		}
	}
	return false
}

var ErrUnauthenticated = errors.New("unauthenticated")
