package api

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/clearsettle/clearsettle/internal/admin"
	"github.com/clearsettle/clearsettle/internal/auth"
	"github.com/clearsettle/clearsettle/internal/payments"
	"github.com/clearsettle/clearsettle/internal/recon"
	"github.com/clearsettle/clearsettle/internal/settle"
	"github.com/clearsettle/clearsettle/internal/store"
)

// Principal is the authenticated caller resolved from either a JWT or an API key.
type Principal struct {
	Kind       string // "user" | "apikey"
	UserID     *uuid.UUID
	Role       string
	MerchantID *uuid.UUID
	Email      string
}

func (p Principal) IsAdmin() bool    { return p.Role == auth.RoleAdmin }
func (p Principal) IsAuditor() bool  { return p.Role == auth.RoleAuditor }
func (p Principal) IsOperator() bool { return p.Role == auth.RoleOperator }

type contextKey string

const ckPrincipal contextKey = "principal"

type Deps struct {
	Pool          *pgxpool.Pool
	Q             *store.Queries
	JWTSecret     string
	SettleHorizon time.Duration
	Admin         *admin.Service
	Payments      *payments.Service
	Settle        *settle.Service
	Recon         *recon.Service
}

func principalFrom(ctx context.Context) Principal {
	if p, ok := ctx.Value(ckPrincipal).(Principal); ok {
		return p
	}
	return Principal{}
}

// clientIP extracts a best-effort IP, honoring the first X-Forwarded-For entry.
func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		if i := strings.IndexByte(xff, ','); i > 0 {
			return strings.TrimSpace(xff[:i])
		}
		return strings.TrimSpace(xff)
	}
	host := r.RemoteAddr
	if i := strings.LastIndexByte(host, ':'); i > 0 {
		return host[:i]
	}
	return host
}
