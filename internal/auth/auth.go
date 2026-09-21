package auth

import (
	"crypto/subtle"
	"net/http"
	"strings"

	"forensiccore/internal/config"

	"github.com/gin-gonic/gin"
)

type ctxKey string

const principalKey ctxKey = "principal"

// Middleware validates "Authorization: Bearer <token>" in constant time and
// stores the resolved principal in the gin context.
func Middleware(principals map[string]config.Principal) gin.HandlerFunc {
	return func(c *gin.Context) {
		h := c.GetHeader("Authorization")
		const prefix = "Bearer "
		if !strings.HasPrefix(h, prefix) || len(h) <= len(prefix) {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "missing bearer token"})
			return
		}
		token := h[len(prefix):]
		var matched config.Principal
		var ok bool
		for t, p := range principals {
			if subtle.ConstantTimeCompare([]byte(t), []byte(token)) == 1 {
				matched, ok = p, true
				break
			}
		}
		if !ok {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "invalid token"})
			return
		}
		c.Set(string(principalKey), matched)
		c.Next()
	}
}

// RequireRole aborts unless the caller has one of the allowed roles.
func RequireRole(roles ...string) gin.HandlerFunc {
	allowed := map[string]bool{}
	for _, r := range roles {
		allowed[r] = true
	}
	return func(c *gin.Context) {
		p, exists := c.Get(string(principalKey))
		if !exists {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "unauthenticated"})
			return
		}
		principal := p.(config.Principal)
		if !allowed[principal.Role] {
			c.AbortWithStatusJSON(http.StatusForbidden, gin.H{
				"error": "role not permitted",
				"role":  principal.Role,
				"need":  roles,
			})
			return
		}
		c.Next()
	}
}

// Principal returns the authenticated principal.
func Principal(c *gin.Context) config.Principal {
	v, _ := c.Get(string(principalKey))
	return v.(config.Principal)
}
