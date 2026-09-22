// Package admin holds admin-only operations: detection-rule versioning and
// employee time-zone maintenance. Both write audit records.
package admin

import (
	"fmt"
	"time"

	"anomalywatch/internal/detection"
	"anomalywatch/internal/models"
	"anomalywatch/internal/timeutil"

	"gorm.io/gorm"
)

// RuleUpdate is the editable portion of a rule.
type RuleUpdate struct {
	Name        *string        `json:"name"`
	Description *string        `json:"description"`
	Enabled     *bool          `json:"enabled"`
	Params      models.JSONMap `json:"params"`
}

// UpdateRule modifies a rule, bumps its version, and records the change in the
// audit log. The version bump means previously evaluated windows are
// re-evaluated by the next sweep under the new definition; existing alerts
// retain the version that fired them.
func UpdateRule(db *gorm.DB, code string, upd RuleUpdate, actor *models.APIKey, now time.Time) (*models.Rule, error) {
	params, err := detection.ValidateParams(code, upd.Params)
	if err != nil {
		return nil, fmt.Errorf("invalid params: %w", err)
	}

	var out *models.Rule
	err = db.Transaction(func(tx *gorm.DB) error {
		var r models.Rule
		if err := tx.Where("code = ? AND active = ?", code, true).First(&r).Error; err != nil {
			if err == gorm.ErrRecordNotFound {
				return fmt.Errorf("rule %q not found", code)
			}
			return err
		}
		before := models.JSONMap{
			"name":        r.Name,
			"description": r.Description,
			"enabled":     r.Enabled,
			"params":      r.Params,
			"version":     r.Version,
		}

		if upd.Name != nil {
			r.Name = *upd.Name
		}
		if upd.Description != nil {
			r.Description = *upd.Description
		}
		if upd.Enabled != nil {
			r.Enabled = *upd.Enabled
		}
		r.Params = params
		r.Version++
		r.UpdatedAt = now
		if err := tx.Save(&r).Error; err != nil {
			return err
		}

		audit := models.AuditLog{
			ActorName:  actorName(actor),
			Action:     "rule.update",
			EntityType: "rule",
			EntityID:   code,
			Detail: models.JSONMap{
				"before": before,
				"after": models.JSONMap{
					"name":        r.Name,
					"description": r.Description,
					"enabled":     r.Enabled,
					"params":      r.Params,
					"version":     r.Version,
				},
			},
			CreatedAt: now,
		}
		if actor != nil {
			audit.ActorKeyID = &actor.ID
		}
		if err := tx.Create(&audit).Error; err != nil {
			return err
		}
		out = &r
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// UpdateTimezone changes an employee's IANA time zone and invalidates the
// time-zone-dependent window evaluations (night/stat), forcing the next sweep
// to recompute affected windows under the new zone.
func UpdateTimezone(db *gorm.DB, employeeID uint64, tz string, actor *models.APIKey, now time.Time) (*models.Employee, error) {
	loc := timeutil.LoadLocation(tz)
	if loc == time.UTC && tz != "" && tz != "UTC" && tz != "Etc/UTC" {
		return nil, fmt.Errorf("unknown time zone %q (use IANA, e.g. Asia/Shanghai)", tz)
	}

	var out *models.Employee
	err := db.Transaction(func(tx *gorm.DB) error {
		var emp models.Employee
		if err := tx.First(&emp, employeeID).Error; err != nil {
			if err == gorm.ErrRecordNotFound {
				return fmt.Errorf("employee %d not found", employeeID)
			}
			return err
		}
		old := emp.TimeZone
		emp.TimeZone = tz
		if err := tx.Save(&emp).Error; err != nil {
			return err
		}
		if err := tx.Where("employee_id = ? AND window_scope IN ?",
			emp.ID, []string{models.ScopeNight, models.ScopeStat}).
			Delete(&models.WindowEvaluation{}).Error; err != nil {
			return err
		}
		audit := models.AuditLog{
			ActorName:  actorName(actor),
			Action:     "employee.timezone",
			EntityType: "employee",
			EntityID:   fmt.Sprintf("%d", emp.ID),
			Detail:     models.JSONMap{"from": old, "to": tz},
			CreatedAt:  now,
		}
		if actor != nil {
			audit.ActorKeyID = &actor.ID
		}
		if err := tx.Create(&audit).Error; err != nil {
			return err
		}
		out = &emp
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

func actorName(a *models.APIKey) string {
	if a == nil {
		return "system"
	}
	return a.Name
}
