// Package httpapi exposes the TargetCraft service over Gin.
package httpapi

import (
	"errors"
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"

	"targetcraft/internal/service"
)

func NewRouter(svc *service.Service) *gin.Engine {
	r := gin.New()
	r.Use(gin.Logger(), gin.Recovery())

	r.GET("/healthz", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"status": "ok"}) })

	v1 := r.Group("/api/v1")
	{
		v1.POST("/campaigns", func(c *gin.Context) {
			var req service.CreateCampaignRequest
			if !bind(c, &req) {
				return
			}
			v, err := svc.CreateCampaign(c.Request.Context(), req)
			respond(c, v, err)
		})
		v1.GET("/campaigns/:id", func(c *gin.Context) {
			id, ok := parseID(c, "id")
			if !ok {
				return
			}
			v, err := svc.GetCampaign(c.Request.Context(), id)
			respond(c, v, err)
		})
		v1.POST("/campaigns/:id/status", func(c *gin.Context) {
			id, ok := parseID(c, "id")
			if !ok {
				return
			}
			var body struct {
				Status string `json:"status" binding:"required"`
			}
			if !bind(c, &body) {
				return
			}
			v, err := svc.SetCampaignStatus(c.Request.Context(), id, body.Status)
			respond(c, v, err)
		})
		v1.POST("/creatives", func(c *gin.Context) {
			var req service.CreateCreativeRequest
			if !bind(c, &req) {
				return
			}
			v, err := svc.CreateCreative(c.Request.Context(), req)
			respond(c, v, err)
		})

		v1.POST("/decisions", func(c *gin.Context) {
			var req service.DecideRequest
			if !bind(c, &req) {
				return
			}
			v, err := svc.Decide(c.Request.Context(), req)
			respond(c, v, err)
		})
		v1.POST("/decisions/:request_id/confirm", func(c *gin.Context) {
			v, err := svc.Confirm(c.Request.Context(), c.Param("request_id"))
			respond(c, v, err)
		})
		v1.GET("/decisions/:request_id", func(c *gin.Context) {
			v, err := svc.GetDecision(c.Request.Context(), c.Param("request_id"))
			respond(c, v, err)
		})
		v1.GET("/decisions", func(c *gin.Context) {
			f := service.DecisionFilter{
				CampaignID: parseUintQuery(c, "campaign_id"),
				UserID:     c.Query("user_id"),
				Status:     c.Query("status"),
				Day:        c.Query("day"),
				Page:       parseIntQuery(c, "page"),
				PageSize:   parseIntQuery(c, "page_size"),
			}
			rows, total, err := svc.ListDecisions(c.Request.Context(), f)
			if err != nil {
				respondError(c, err)
				return
			}
			c.JSON(http.StatusOK, gin.H{"data": rows, "total": total})
		})
		v1.GET("/settlements", func(c *gin.Context) {
			f := service.SettlementFilter{
				CampaignID: parseUintQuery(c, "campaign_id"),
				Day:        c.Query("day"),
				Type:       c.Query("type"),
				Page:       parseIntQuery(c, "page"),
				PageSize:   parseIntQuery(c, "page_size"),
			}
			rows, total, err := svc.ListSettlements(c.Request.Context(), f)
			if err != nil {
				respondError(c, err)
				return
			}
			c.JSON(http.StatusOK, gin.H{"data": rows, "total": total})
		})
	}
	return r
}

func bind(c *gin.Context, v any) bool {
	if err := c.ShouldBindJSON(v); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"code": "INVALID_BODY", "message": err.Error()}})
		return false
	}
	return true
}

func respond(c *gin.Context, v any, err error) {
	if err != nil {
		respondError(c, err)
		return
	}
	c.JSON(http.StatusOK, v)
}

func respondError(c *gin.Context, err error) {
	var apiErr *service.APIError
	if errors.As(err, &apiErr) {
		c.JSON(apiErr.Status, gin.H{"error": gin.H{"code": apiErr.Code, "message": apiErr.Message}})
		return
	}
	c.JSON(http.StatusInternalServerError, gin.H{"error": gin.H{"code": "INTERNAL", "message": err.Error()}})
}

func parseID(c *gin.Context, param string) (uint64, bool) {
	id, err := strconv.ParseUint(c.Param(param), 10, 64)
	if err != nil || id == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"code": "INVALID_ID", "message": "invalid " + param}})
		return 0, false
	}
	return id, true
}

func parseUintQuery(c *gin.Context, key string) uint64 {
	v, _ := strconv.ParseUint(c.Query(key), 10, 64)
	return v
}

func parseIntQuery(c *gin.Context, key string) int {
	v, _ := strconv.Atoi(c.Query(key))
	return v
}
