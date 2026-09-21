package api

import (
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"targetcraft/internal/models"
	"targetcraft/internal/service"
)

type Handler struct {
	svc *service.Service
	db  *gorm.DB
}

func NewRouter(svc *service.Service, db *gorm.DB) *gin.Engine {
	r := gin.New()
	r.Use(gin.Logger(), gin.Recovery())

	h := &Handler{svc: svc, db: db}

	r.GET("/api/health", h.health)

	r.POST("/api/campaigns", h.createCampaign)
	r.GET("/api/campaigns/:id", h.getCampaign)
	r.POST("/api/campaigns/:id/pause", h.pauseCampaign)
	r.POST("/api/campaigns/:id/resume", h.resumeCampaign)

	r.POST("/api/campaigns/:id/creatives", h.createCreative)
	r.POST("/api/creatives/:id/pause", h.pauseCreative)
	r.POST("/api/creatives/:id/resume", h.resumeCreative)

	r.POST("/api/decisions", h.decide)
	r.GET("/api/decisions", h.listDecisions)
	r.GET("/api/decisions/:request_id", h.getDecision)
	r.POST("/api/decisions/:request_id/confirm", h.confirm)

	r.GET("/api/settlements", h.listSettlements)

	return r
}

func (h *Handler) health(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"status": "ok", "time": time.Now().UTC()})
}

// ---- campaigns ----

type createCampaignReq struct {
	Name        string       `json:"name" binding:"required"`
	StartAt     time.Time    `json:"start_at" binding:"required"`
	EndAt       time.Time    `json:"end_at" binding:"required"`
	TotalBudget int64        `json:"total_budget" binding:"required,gt=0"`
	DailyCap    int64        `json:"daily_cap"`
	Rules       models.Rules `json:"rules"`
}

func (h *Handler) createCampaign(c *gin.Context) {
	var req createCampaignReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	campaign := models.Campaign{
		Name:        req.Name,
		Status:      models.CampaignStatusActive,
		StartAt:     req.StartAt.UTC(),
		EndAt:       req.EndAt.UTC(),
		TotalBudget: req.TotalBudget,
		DailyCap:    req.DailyCap,
		Rules:       req.Rules.Marshal(),
	}
	if err := h.db.Create(&campaign).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusCreated, gin.H{"campaign": campaign})
}

func (h *Handler) getCampaign(c *gin.Context) {
	id, _ := strconv.ParseUint(c.Param("id"), 10, 64)
	var campaign models.Campaign
	if err := h.db.First(&campaign, id).Error; err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "campaign not found"})
		return
	}
	day := time.Now().UTC().Format("2006-01-02")
	var daily models.DailyBudget
	_ = h.db.Where("campaign_id = ? AND day = ?", campaign.ID, day).First(&daily).Error
	c.JSON(http.StatusOK, gin.H{
		"campaign":     campaign,
		"rules":        models.ParseRules(campaign.Rules),
		"daily_budget": daily,
	})
}

func (h *Handler) pauseCampaign(c *gin.Context)  { h.setCampaignStatus(c, models.CampaignStatusPaused) }
func (h *Handler) resumeCampaign(c *gin.Context) { h.setCampaignStatus(c, models.CampaignStatusActive) }

func (h *Handler) setCampaignStatus(c *gin.Context, status string) {
	id, _ := strconv.ParseUint(c.Param("id"), 10, 64)
	if err := h.svc.SetCampaignStatus(c.Request.Context(), id, status); err != nil {
		if errors.Is(err, service.ErrNotFound) {
			c.JSON(http.StatusNotFound, gin.H{"error": "campaign not found"})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"id": id, "status": status})
}

// ---- creatives ----

type createCreativeReq struct {
	Name    string `json:"name" binding:"required"`
	Content string `json:"content"`
}

func (h *Handler) createCreative(c *gin.Context) {
	campaignID, _ := strconv.ParseUint(c.Param("id"), 10, 64)
	var req createCreativeReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	var campaign models.Campaign
	if err := h.db.First(&campaign, campaignID).Error; err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "campaign not found"})
		return
	}
	creative := models.Creative{
		CampaignID: campaign.ID,
		Name:       req.Name,
		Content:    req.Content,
		Status:     models.CreativeStatusActive,
	}
	if err := h.db.Create(&creative).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusCreated, gin.H{"creative": creative})
}

func (h *Handler) pauseCreative(c *gin.Context)  { h.setCreativeStatus(c, models.CreativeStatusPaused) }
func (h *Handler) resumeCreative(c *gin.Context) { h.setCreativeStatus(c, models.CreativeStatusActive) }

func (h *Handler) setCreativeStatus(c *gin.Context, status string) {
	id, _ := strconv.ParseUint(c.Param("id"), 10, 64)
	r := h.db.Model(&models.Creative{}).Where("id = ?", id).Update("status", status)
	if r.RowsAffected == 0 {
		c.JSON(http.StatusNotFound, gin.H{"error": "creative not found"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"id": id, "status": status})
}

// ---- decisions ----

func (h *Handler) decide(c *gin.Context) {
	var req service.DecideRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	result, err := h.svc.Decide(c.Request.Context(), req)
	if err != nil {
		if errors.Is(err, service.ErrConflict) {
			c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
			return
		}
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	status := http.StatusOK
	if result.Approved {
		status = http.StatusCreated
	}
	c.JSON(status, result)
}

func (h *Handler) getDecision(c *gin.Context) {
	d, err := h.svc.GetDecision(c.Request.Context(), c.Param("request_id"))
	if err != nil {
		if errors.Is(err, service.ErrNotFound) {
			c.JSON(http.StatusNotFound, gin.H{"error": "decision not found"})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"decision": d})
}

func (h *Handler) listDecisions(c *gin.Context) {
	campaignID, _ := strconv.ParseUint(c.Query("campaign_id"), 10, 64)
	limit, _ := strconv.Atoi(c.Query("limit"))
	out, err := h.svc.ListDecisions(c.Request.Context(), campaignID, c.Query("user_id"), c.Query("status"), limit)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"decisions": out})
}

func (h *Handler) confirm(c *gin.Context) {
	d, err := h.svc.Confirm(c.Request.Context(), c.Param("request_id"))
	if err != nil {
		switch {
		case errors.Is(err, service.ErrNotFound):
			c.JSON(http.StatusNotFound, gin.H{"error": "decision not found"})
		case errors.Is(err, service.ErrExpired):
			c.JSON(http.StatusGone, gin.H{"error": "reservation expired"})
		case errors.Is(err, service.ErrReleased):
			c.JSON(http.StatusGone, gin.H{"error": "reservation already released"})
		default:
			c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
		}
		return
	}
	c.JSON(http.StatusOK, gin.H{"settlement": d})
}

func (h *Handler) listSettlements(c *gin.Context) {
	campaignID, _ := strconv.ParseUint(c.Query("campaign_id"), 10, 64)
	limit, _ := strconv.Atoi(c.Query("limit"))
	out, err := h.svc.ListSettlements(c.Request.Context(), campaignID, c.Query("day"), limit)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"settlements": out})
}
