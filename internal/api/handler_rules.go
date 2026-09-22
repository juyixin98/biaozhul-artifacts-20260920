package api

import (
	"encoding/json"
	"net/http"

	"activityguard/internal/audit"
	"activityguard/internal/authctx"
	"activityguard/internal/models"
	"activityguard/internal/util"

	"github.com/gin-gonic/gin"
	"gorm.io/datatypes"
	"gorm.io/gorm"
)

var knownRuleKeys = map[string]bool{
	models.RuleDownloadBurst: true,
	models.RuleFirstUSB:      true,
	models.RuleNightActivity: true,
	models.RuleZScore:        true,
}

func (s *Server) listRules(c *gin.Context) {
	var rules []models.DetectionRule
	q := s.db.Order("rule_key ASC, version DESC")
	if c.Query("active_only") == "true" {
		q = q.Where("is_active = ?", true)
	}
	if err := q.Find(&rules).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "list rules"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": rules})
}

func (s *Server) getRule(c *gin.Context) {
	key := c.Param("key")
	if !knownRuleKeys[key] {
		c.JSON(http.StatusNotFound, gin.H{"error": "unknown rule key"})
		return
	}
	var rules []models.DetectionRule
	if err := s.db.Where("rule_key = ?", key).Order("version DESC").Find(&rules).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "get rule"})
		return
	}
	if len(rules) == 0 {
		c.JSON(http.StatusNotFound, gin.H{"error": "rule not found"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": rules, "active": rules[0].IsActive && firstActive(rules)})
}

func firstActive(rs []models.DetectionRule) bool {
	// listRules 排序 version DESC，第一条即最高版本；是否激活由它自身 is_active 表示。
	for _, r := range rs {
		if r.IsActive {
			return true
		}
	}
	return false
}

type createRuleVersionRequest struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	Params      map[string]any `json:"params" binding:"required"`
}

// createRuleVersion 为指定 rule_key 追加一个新版本并将其激活，旧版本停用。
// 规则修改写审计；已有告警保留其 rule_version 与依据，不受新版本影响。
func (s *Server) createRuleVersion(c *gin.Context) {
	ident := c.MustGet("identity").(authctx.Identity)
	key := c.Param("key")
	if !knownRuleKeys[key] {
		c.JSON(http.StatusNotFound, gin.H{"error": "unknown rule key"})
		return
	}
	var req createRuleVersionRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "params object is required"})
		return
	}
	if err := validateRuleParams(key, req.Params); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	var rule models.DetectionRule
	err := s.db.Transaction(func(tx *gorm.DB) error {
		var latest models.DetectionRule
		dbErr := tx.Where("rule_key = ?", key).Order("version DESC").Limit(1).First(&latest).Error
		nextVersion := 1
		if dbErr == nil {
			nextVersion = latest.Version + 1
			if req.Name == "" {
				req.Name = latest.Name
			}
			if req.Description == "" {
				req.Description = latest.Description
			}
		} else if dbErr != gorm.ErrRecordNotFound {
			return dbErr
		}
		params, _ := json.Marshal(req.Params)
		rule = models.DetectionRule{
			ID:          util.NewID(),
			RuleKey:     key,
			Version:     nextVersion,
			Name:        req.Name,
			Description: req.Description,
			Params:      datatypes.JSON(params),
			IsActive:    true,
			CreatedBy:   &ident.UserID,
		}
		if err := tx.Create(&rule).Error; err != nil {
			return err
		}
		if err := tx.Model(&models.DetectionRule{}).
			Where("rule_key = ? AND id <> ?", key, rule.ID).
			Update("is_active", false).Error; err != nil {
			return err
		}
		return audit.InTx(tx, &ident, "rule.create_version", "detection_rule", key, map[string]any{
			"rule_key": key, "version": nextVersion, "params": req.Params,
		})
	})
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "create rule version: " + err.Error()})
		return
	}
	c.JSON(http.StatusCreated, rule)
}

func validateRuleParams(key string, p map[string]any) error {
	switch key {
	case models.RuleDownloadBurst:
		return requirePositiveNumbers(p, "window_minutes", "threshold")
	case models.RuleNightActivity:
		return requireHourRange(p)
	case models.RuleZScore:
		if err := requirePositiveNumbers(p, "lookback_days", "min_sample_days", "min_positive_days"); err != nil {
			return err
		}
		if z, ok := p["z_threshold"].(float64); !ok || z <= 0 {
			return errField("z_threshold must be a positive number")
		}
		return nil
	case models.RuleFirstUSB:
		return nil
	}
	return errField("unknown rule")
}
