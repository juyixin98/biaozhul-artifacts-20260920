package middleware

import (
	"net/http"

	"proofcycle/internal/auth"
	"proofcycle/internal/models"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

// CurrentUser is the gin context key for the authenticated user.
const CurrentUser = "currentUser"

// AuthRequired validates the bearer token and loads the user.
func AuthRequired(gdb *gorm.DB) gin.HandlerFunc {
	return func(c *gin.Context) {
		token := auth.ExtractToken(c.GetHeader("Authorization"))
		if token == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "missing bearer token"})
			return
		}
		u, err := auth.ResolveToken(gdb, token)
		if err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "invalid or expired token"})
			return
		}
		c.Set(CurrentUser, u)
		c.Next()
	}
}

// User returns the authenticated user from the context.
func User(c *gin.Context) *models.User {
	v, ok := c.Get(CurrentUser)
	if !ok {
		return nil
	}
	u, _ := v.(*models.User)
	return u
}

// RequireRole rejects users whose role is not among the allowed ones.
func RequireRole(roles ...string) gin.HandlerFunc {
	allowed := make(map[string]struct{}, len(roles))
	for _, r := range roles {
		allowed[r] = struct{}{}
	}
	return func(c *gin.Context) {
		u := User(c)
		if u == nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "unauthenticated"})
			return
		}
		if _, ok := allowed[u.Role]; !ok {
			c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"error": "your role may not perform this action"})
			return
		}
		c.Next()
	}
}
