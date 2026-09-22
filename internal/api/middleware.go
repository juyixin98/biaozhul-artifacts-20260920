package api

import (
	"net/http"
	"strings"

	"geoterritory/internal/models"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

const orgContextKey = "org"

// AuthMiddleware resolves the caller's organization from the X-API-Key
// header. There is deliberately no broader user model: authorization unit is
// the organization, and every downstream query is scoped by its id. Requests
// without a valid key get 401 and reach no data.
func AuthMiddleware(db *gorm.DB) gin.HandlerFunc {
	return func(c *gin.Context) {
		key := strings.TrimSpace(c.GetHeader("X-API-Key"))
		if key == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "missing X-API-Key header"})
			return
		}
		var org models.Organization
		if err := db.Where("api_key = ?", key).First(&org).Error; err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "invalid API key"})
			return
		}
		c.Set(orgContextKey, &org)
		c.Next()
	}
}

func currentOrg(c *gin.Context) *models.Organization {
	v, ok := c.Get(orgContextKey)
	if !ok {
		c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "unauthenticated"})
		return nil
	}
	return v.(*models.Organization)
}
