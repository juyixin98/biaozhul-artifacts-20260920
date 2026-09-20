package httpapi

import (
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"proofcycle/internal/model"
	"proofcycle/internal/service"
)

const currentUserKey = "currentUser"

// AuthRequired authenticates the static per-user API token in X-API-Token.
func AuthRequired(svc *service.Service) gin.HandlerFunc {
	return func(c *gin.Context) {
		token := strings.TrimSpace(c.GetHeader("X-API-Token"))
		if token == "" {
			auth := c.GetHeader("Authorization")
			if strings.HasPrefix(auth, "Bearer ") {
				token = strings.TrimSpace(strings.TrimPrefix(auth, "Bearer "))
			}
		}
		if token == "" {
			abortServiceError(c, &service.Error{Status: http.StatusUnauthorized, Code: "unauthorized", Message: "missing X-API-Token"})
			return
		}
		user, err := svc.UserByToken(c.Request.Context(), token)
		if err != nil {
			abortServiceError(c, err)
			return
		}
		c.Set(currentUserKey, user)
		c.Next()
	}
}

func currentUser(c *gin.Context) *model.User {
	v, ok := c.Get(currentUserKey)
	if !ok {
		return nil
	}
	return v.(*model.User)
}

// BootstrapRequired gates user provisioning. When the server has no bootstrap
// token configured, self-service user creation is disabled.
func BootstrapRequired(expected string) gin.HandlerFunc {
	return func(c *gin.Context) {
		if expected == "" {
			c.AbortWithStatusJSON(http.StatusForbidden, gin.H{
				"code":    "bootstrap_disabled",
				"message": "user creation is disabled; seed users via PROOFCYCLE_SEED_DEMO",
			})
			return
		}
		got := c.GetHeader("X-Bootstrap-Token")
		if got != expected {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"code":    "unauthorized",
				"message": "invalid bootstrap token",
			})
			return
		}
		c.Next()
	}
}
