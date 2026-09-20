// Package middleware 提供基于 X-User-ID 请求头的简易身份识别。
package middleware

import (
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"proofcycle/internal/models"
)

const UserKey = "current_user"

// Auth 解析 X-User-ID 并加载用户；缺失或未知用户一律 401。
func Auth(db *gorm.DB) gin.HandlerFunc {
	return func(c *gin.Context) {
		h := c.GetHeader("X-User-ID")
		if h == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "missing X-User-ID header"})
			return
		}
		id, err := strconv.ParseUint(h, 10, 64)
		if err != nil || id == 0 {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "invalid X-User-ID header"})
			return
		}
		var u models.User
		if err := db.First(&u, id).Error; err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "unknown user"})
			return
		}
		c.Set(UserKey, &u)
		c.Next()
	}
}

// CurrentUser 取出已认证用户。
func CurrentUser(c *gin.Context) *models.User {
	v, ok := c.Get(UserKey)
	if !ok {
		return nil
	}
	u, _ := v.(*models.User)
	return u
}
