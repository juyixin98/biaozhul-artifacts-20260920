package main

import (
	"net/http"

	"geoterritory/internal/api"
	"geoterritory/internal/service"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

func newRouter(db *gorm.DB) http.Handler {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(requestLogger())

	r.GET("/health", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"status": "ok"}) })

	authed := r.Group("/api/v1")
	authed.Use(api.AuthMiddleware(db))

	h := api.NewHandler(
		service.NewRegionService(db),
		service.NewPointService(db),
	)
	h.Register(authed)

	return r
}

func requestLogger() gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Next()
	}
}
