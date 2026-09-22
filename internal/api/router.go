package api

import (
	"net/http"
	"strings"

	"activityguard/internal/auth"
	"activityguard/internal/authctx"
	"activityguard/internal/config"
	"activityguard/internal/detection"
	"activityguard/internal/models"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

type Server struct {
	db     *gorm.DB
	cfg    config.Config
	engine *detection.Engine
}

func NewServer(gdb *gorm.DB, cfg config.Config, engine *detection.Engine) *Server {
	return &Server{db: gdb, cfg: cfg, engine: engine}
}

func (s *Server) Router() *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Logger(), gin.Recovery())

	r.GET("/healthz", func(c *gin.Context) {
		sqlDB, err := s.db.DB()
		if err != nil || sqlDB.Ping() != nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"status": "degraded"})
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "ok"})
	})

	v1 := r.Group("/api/v1")
	{
		v1.POST("/auth/login", s.login)

		authed := v1.Group("")
		authed.Use(s.authMiddleware())
		{
			authed.POST("/events/batch", s.ingestEvents)

			authed.GET("/alerts", s.listAlerts)
			authed.GET("/alerts/:id", s.getAlert)
			authed.POST("/alerts/:id/transition", s.transitionAlert)

			authed.GET("/departments", s.listDepartments)
			authed.GET("/employees", s.listEmployees)

			authed.GET("/rules", s.listRules)
			authed.GET("/rules/:key", s.getRule)

			// 仅管理员
			admin := authed.Group("")
			admin.Use(s.requireAdmin())
			{
				admin.POST("/rules/:key/versions", s.createRuleVersion)
				admin.POST("/employees/:id/timezone", s.updateEmployeeTimezone)
				admin.GET("/audit-logs", s.listAuditLogs)
			}
		}
	}
	return r
}

func (s *Server) authMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		h := c.GetHeader("Authorization")
		if h == "" || !strings.HasPrefix(h, "Bearer ") {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "missing bearer token"})
			return
		}
		token := strings.TrimSpace(strings.TrimPrefix(h, "Bearer "))
		claims, err := auth.ParseToken(s.cfg.JWTSecret, token)
		if err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "invalid or expired token"})
			return
		}
		var user models.User
		if err := s.db.Where("id = ? AND is_active = ?", claims.UserID, true).First(&user).Error; err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "user disabled or not found"})
			return
		}
		id := authctx.Identity{UserID: user.ID, Username: user.Username, Role: user.Role}
		if user.Role == "analyst" {
			var deptIDs []string
			if err := s.db.Model(&models.UserDepartment{}).
				Where("user_id = ?", user.ID).Pluck("department_id", &deptIDs).Error; err != nil {
				c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{"error": "load departments"})
				return
			}
			id.DepartmentIDs = deptIDs
		}
		c.Set("identity", id)
		c.Next()
	}
}

func (s *Server) requireAdmin() gin.HandlerFunc {
	return func(c *gin.Context) {
		id, _ := c.Get("identity")
		ident := id.(authctx.Identity)
		if !ident.IsAdmin() {
			c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"error": "admin role required"})
			return
		}
		c.Next()
	}
}
