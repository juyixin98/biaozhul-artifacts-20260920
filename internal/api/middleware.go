package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"strings"

	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/auth"
)

func requestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rid := r.Header.Get("X-Request-Id")
		if rid == "" {
			b := make([]byte, 8)
			_, _ = rand.Read(b)
			rid = hex.EncodeToString(b)
		}
		w.Header().Set("X-Request-Id", rid)
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), requestIDKey, rid)))
	})
}

type ctxKey string

const requestIDKey ctxKey = "rid"

// bearer extracts a token from "Authorization: Bearer <t>".
func bearer(r *http.Request) string {
	h := r.Header.Get("Authorization")
	if h == "" {
		return ""
	}
	if len(h) > 7 && strings.EqualFold(h[:7], "Bearer ") {
		return strings.TrimSpace(h[7:])
	}
	return ""
}

// authenticateUser accepts only JWT users.
func (d Deps) authenticateUser(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tok := bearer(r)
		if tok == "" {
			writeError(w, http.StatusUnauthorized, "missing_token", "Authorization: Bearer <jwt> required")
			return
		}
		claims, err := auth.ParseToken(d.JWTSecret, tok)
		if err != nil {
			writeError(w, http.StatusUnauthorized, "invalid_token", err.Error())
			return
		}
		p := Principal{Kind: "user", Role: claims.Role, Email: claims.Email}
		if claims.Sub != "" {
			if uid, perr := uuid.Parse(claims.Sub); perr == nil {
				p.UserID = &uid
			}
		}
		if claims.MerchantID != "" {
			if mid, perr := uuid.Parse(claims.MerchantID); perr == nil {
				p.MerchantID = &mid
			}
		}
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ckPrincipal, p)))
	})
}

// authenticateWrite accepts either an operator JWT or an operator API key.
func (d Deps) authenticateWrite(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tok := bearer(r)
		if tok != "" && !strings.HasPrefix(tok, "cs_live_") {
			claims, err := auth.ParseToken(d.JWTSecret, tok)
			if err != nil {
				writeError(w, http.StatusUnauthorized, "invalid_token", err.Error())
				return
			}
			p := Principal{Kind: "user", Role: claims.Role, Email: claims.Email}
			if claims.Sub != "" {
				if uid, perr := uuid.Parse(claims.Sub); perr == nil {
					p.UserID = &uid
				}
			}
			if claims.MerchantID != "" {
				if mid, perr := uuid.Parse(claims.MerchantID); perr == nil {
					p.MerchantID = &mid
				}
			}
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ckPrincipal, p)))
			return
		}
		// API key path: Authorization: Bearer cs_live_... or X-Api-Key.
		key := tok
		if key == "" {
			key = r.Header.Get("X-Api-Key")
		}
		if key == "" {
			writeError(w, http.StatusUnauthorized, "missing_credentials", "Bearer JWT/API key or X-Api-Key required")
			return
		}
		m, err := d.Admin.AuthenticateAPIKey(r.Context(), key)
		if err != nil {
			writeError(w, http.StatusUnauthorized, "invalid_api_key", "unknown or revoked API key")
			return
		}
		mid := m.ID
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ckPrincipal, Principal{
			Kind: "apikey", Role: auth.RoleOperator, MerchantID: &mid,
		})))
	})
}

// authenticateRead accepts an operator JWT, an auditor/admin JWT, or an
// operator API key.
func (d Deps) authenticateRead(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tok := bearer(r)
		if tok != "" && !strings.HasPrefix(tok, "cs_live_") {
			claims, err := auth.ParseToken(d.JWTSecret, tok)
			if err != nil {
				writeError(w, http.StatusUnauthorized, "invalid_token", err.Error())
				return
			}
			if claims.Role != auth.RoleOperator &&
				claims.Role != auth.RoleAuditor && claims.Role != auth.RoleAdmin {
				writeError(w, http.StatusForbidden, "forbidden_role", "operator or auditor required")
				return
			}
			p := Principal{Kind: "user", Role: claims.Role, Email: claims.Email}
			if claims.Sub != "" {
				if uid, perr := uuid.Parse(claims.Sub); perr == nil {
					p.UserID = &uid
				}
			}
			if claims.MerchantID != "" {
				if mid, perr := uuid.Parse(claims.MerchantID); perr == nil {
					p.MerchantID = &mid
				}
			}
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ckPrincipal, p)))
			return
		}
		key := tok
		if key == "" {
			key = r.Header.Get("X-Api-Key")
		}
		if key == "" {
			writeError(w, http.StatusUnauthorized, "missing_credentials", "Bearer JWT/API key or X-Api-Key required")
			return
		}
		m, err := d.Admin.AuthenticateAPIKey(r.Context(), key)
		if err != nil {
			writeError(w, http.StatusUnauthorized, "invalid_api_key", "unknown or revoked API key")
			return
		}
		mid := m.ID
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ckPrincipal, Principal{
			Kind: "apikey", Role: auth.RoleOperator, MerchantID: &mid,
		})))
	})
}

// merchantScope forces a resolved merchant onto the request and rejects JWT
// operators without a merchant assignment.
func (d Deps) merchantScope(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := principalFrom(r.Context())
		if p.MerchantID == nil {
			writeError(w, http.StatusForbidden, "no_merchant", "operator is not bound to a merchant")
			return
		}
		next.ServeHTTP(w, r)
	})
}

func requireRole(role string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			p := principalFrom(r.Context())
			if p.Role != role {
				writeError(w, http.StatusForbidden, "forbidden_role", "requires role "+role)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

func requireAnyRole(roles ...string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			p := principalFrom(r.Context())
			for _, want := range roles {
				if p.Role == want {
					next.ServeHTTP(w, r)
					return
				}
			}
			writeError(w, http.StatusForbidden, "forbidden_role", "requires one of "+strings.Join(roles, ","))
		})
	}
}
