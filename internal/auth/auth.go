// Package auth authenticates API keys and carries the principal + org scope.
package auth

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"

	"costlens/internal/db/dbgen"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

type Role string

const (
	RoleAdmin   Role = "admin"
	RoleAnalyst Role = "analyst"
	RoleViewer  Role = "viewer"
)

type Principal struct {
	ID     string
	Login  string
	Name   string
	Role   Role
	OrgIDs []string // organizations the user may access; admins implicitly get all
}

func (p *Principal) CanImport() bool { return p.Role == RoleAdmin || p.Role == RoleAnalyst }

// CanAccessOrg is the single gate used by every detail/summary/export query.
func (p *Principal) CanAccessOrg(orgID string) bool {
	if p.Role == RoleAdmin {
		return true
	}
	for _, o := range p.OrgIDs {
		if o == orgID {
			return true
		}
	}
	return false
}

// ScopeOrgIDs returns the uuid array to bind to a scoped query.
func (p *Principal) ScopeOrgIDs() []string {
	out := make([]string, len(p.OrgIDs))
	copy(out, p.OrgIDs)
	return out
}

type contextKey struct{}

// WithPrincipal stores the principal on the request context.
func WithPrincipal(ctx context.Context, p *Principal) context.Context {
	return context.WithValue(ctx, contextKey{}, p)
}

// FromContext returns the principal placed by the middleware.
func FromContext(ctx context.Context) (*Principal, error) {
	p, ok := ctx.Value(contextKey{}).(*Principal)
	if !ok || p == nil {
		return nil, errors.New("unauthenticated")
	}
	return p, nil
}

// HashKey returns the stable SHA-256 hex hash of an API key.
func HashKey(raw string) string {
	sum := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(sum[:])
}

// Authenticate resolves an API key (scheme: "Bearer <key>") to a principal.
func Authenticate(ctx context.Context, db dbgen.DBTX, rawHeader string) (*Principal, error) {
	const prefix = "Bearer "
	if len(rawHeader) <= len(prefix) {
		return nil, errors.New("missing bearer token")
	}
	key := rawHeader[len(prefix):]
	if key == "" {
		return nil, errors.New("empty api key")
	}
	q := dbgen.New(db)
	row, err := q.AuthByAPIKeyHash(ctx, HashKey(key))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, errors.New("invalid api key")
		}
		return nil, err
	}
	p := &Principal{
		ID:     row.ID,
		Login:  row.Login,
		Name:   row.FullName,
		Role:   Role(row.Role),
		OrgIDs: row.OrgIds,
	}
	// Admins implicitly have access to every organization: load the full set
	// so the same ANY(org_ids) scope filter applies uniformly.
	if p.Role == RoleAdmin {
		orgs, err := q.ListOrganizations(ctx)
		if err != nil {
			return nil, err
		}
		p.OrgIDs = make([]string, 0, len(orgs))
		for _, o := range orgs {
			p.OrgIDs = append(p.OrgIDs, o.ID)
		}
	}
	return p, nil
}

// TextNull builds a nullable uuid/text bind value.
func TextNull(s string) pgtype.Text {
	if s == "" {
		return pgtype.Text{Valid: false}
	}
	return pgtype.Text{String: s, Valid: true}
}
