// Package auth provides static Bearer-token authentication with two roles:
// investigator (register, transfer, note, query) and analyst (query, note).
package auth

import (
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
)

// Roles.
const (
	RoleInvestigator = "investigator"
	RoleAnalyst      = "analyst"
)

const actorContextKey = "forensiccore.actor"

// Actor describes the authenticated caller.
type Actor struct {
	Role  string
	Token string
}

// Name returns a stable display name for audit fields.
func (a Actor) Name() string {
	return "role:" + a.Role
}

// Middleware validates the Authorization header and enforces allowedRoles.
func Middleware(investigatorToken, analystToken string, allowedRoles ...string) gin.HandlerFunc {
	tokenToRole := map[string]string{
		investigatorToken: RoleInvestigator,
		analystToken:      RoleAnalyst,
	}
	allowed := make(map[string]bool, len(allowedRoles))
	for _, r := range allowedRoles {
		allowed[r] = true
	}
	return func(c *gin.Context) {
		h := c.GetHeader("Authorization")
		if h == "" || !strings.HasPrefix(h, "Bearer ") {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"error": "missing or malformed Authorization: Bearer token required",
			})
			return
		}
		token := strings.TrimSpace(strings.TrimPrefix(h, "Bearer "))
		role, ok := tokenToRole[token]
		if !ok {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "invalid token"})
			return
		}
		if !allowed[role] {
			c.AbortWithStatusJSON(http.StatusForbidden, gin.H{
				"error": "role " + role + " is not permitted to perform this action",
			})
			return
		}
		c.Set(actorContextKey, Actor{Role: role, Token: token})
		c.Next()
	}
}

// FromContext returns the authenticated actor.
func FromContext(c *gin.Context) Actor {
	v, ok := c.Get(actorContextKey)
	if !ok {
		return Actor{}
	}
	return v.(Actor)
}
