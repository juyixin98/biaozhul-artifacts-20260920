// Package api implements the GeoTerritory HTTP API (Gin).
package api

import (
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"geoterritory/internal/config"
	"geoterritory/internal/models"
	"geoterritory/internal/store"
)

// Server bundles HTTP dependencies.
type Server struct {
	db  *gorm.DB
	cfg config.Config
}

// NewServer creates the API server.
func NewServer(db *gorm.DB, cfg config.Config) *Server {
	return &Server{db: db, cfg: cfg}
}

// Router builds the gin engine with all routes and middleware.
func (s *Server) Router() *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(requestLogger())

	r.GET("/healthz", func(c *gin.Context) {
		sqlDB, err := s.db.DB()
		if err != nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"status": "db-error"})
			return
		}
		if err := sqlDB.Ping(); err != nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"status": "db-unreachable"})
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "ok"})
	})

	v1 := r.Group("/v1", s.authMiddleware())
	{
		v1.GET("/catalog", s.getCatalog)

		v1.POST("/regions", s.publishRegion)
		v1.GET("/regions", s.listRegions)
		v1.GET("/regions/:id", s.getRegion)
		v1.GET("/regions/:id/versions/:version", s.getRegionVersion)
		v1.POST("/regions/:id/deactivate", s.deactivateRegion)

		v1.POST("/points/batch", s.batchPoints)
		v1.GET("/points/bbox", s.bboxQuery)
		v1.GET("/points/nearest", s.nearestQuery)
		v1.GET("/points/:external_id", s.getPoint)

		v1.GET("/jobs/:id", s.getJob)
	}
	return r
}

func requestLogger() gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Next()
	}
}

type orgContextKey struct{}

// authMiddleware resolves the organization API key. Every downstream query is
// filtered by that org id: cross-organization data can never be returned.
func (s *Server) authMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		key := c.GetHeader("X-API-Key")
		if key == "" {
			if a := c.GetHeader("Authorization"); strings.HasPrefix(a, "Bearer ") {
				key = strings.TrimPrefix(a, "Bearer ")
			}
		}
		if key == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "missing API key"})
			return
		}
		org, err := store.OrganizationByAPIKey(s.db, key)
		if err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "invalid API key"})
			return
		}
		c.Set("org", org)
		c.Next()
	}
}

func orgFrom(c *gin.Context) *models.Organization {
	v, _ := c.Get("org")
	return v.(*models.Organization)
}
