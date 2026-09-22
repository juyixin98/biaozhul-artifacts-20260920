// Package httpapi exposes the REST API, including API-key authentication and
// department-scoped RBAC (analysts see only assigned departments; admins
// manage rules and employee time zones).
package httpapi

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"

	"anomalywatch/internal/models"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

const contextKey = "auth.api_key"

func hashKey(raw string) string {
	return HashKeyExported(raw)
}

// HashKeyExported returns the stored hash form of a raw API key.
func HashKeyExported(raw string) string {
	sum := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(sum[:])
}

// Auth middleware resolves the X-API-Key header to an active api_keys row.
func Auth(db *gorm.DB) gin.HandlerFunc {
	return func(c *gin.Context) {
		raw := strings.TrimSpace(c.GetHeader("X-API-Key"))
		if raw == "" {
			raw = strings.TrimSpace(c.Query("api_key"))
		}
		if raw == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "missing X-API-Key"})
			return
		}
		var key models.APIKey
		err := db.Where("key_hash = ? AND active = ?", hashKey(raw), true).First(&key).Error
		if err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "invalid API key"})
			return
		}
		c.Set(contextKey, &key)
		c.Next()
	}
}

func currentKey(c *gin.Context) *models.APIKey {
	v, ok := c.Get(contextKey)
	if !ok {
		return nil
	}
	return v.(*models.APIKey)
}

// RequireRole returns middleware that admits only the given roles.
func RequireRole(roles ...string) gin.HandlerFunc {
	allowed := map[string]bool{}
	for _, r := range roles {
		allowed[r] = true
	}
	return func(c *gin.Context) {
		key := currentKey(c)
		if key == nil || !allowed[key.Role] {
			c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"error": "insufficient role"})
			return
		}
		c.Next()
	}
}
