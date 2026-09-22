package alerts

import (
	"fmt"
	"time"

	"anomalywatch/internal/detection"
	"anomalywatch/internal/models"

	"gorm.io/gorm"
)

// allowedTransitions defines the alert state machine.
var allowedTransitions = map[string]map[string]bool{
	models.AlertStatusNew: {
		models.AlertStatusInvestigating: true,
		models.AlertStatusEscalated:     true,
		models.AlertStatusResolved:      true,
		models.AlertStatusFalsePositive: true,
	},
	models.AlertStatusInvestigating: {
		models.AlertStatusEscalated:     true,
		models.AlertStatusResolved:      true,
		models.AlertStatusFalsePositive: true,
	},
	models.AlertStatusEscalated: {
		models.AlertStatusInvestigating: true,
		models.AlertStatusResolved:      true,
		models.AlertStatusFalsePositive: true,
	},
	models.AlertStatusResolved:      {},
	models.AlertStatusFalsePositive: {},
}

// InvalidTransitionError reports a disallowed status change.
type InvalidTransitionError struct{ From, To string }

func (e *InvalidTransitionError) Error() string {
	return fmt.Sprintf("transition %s -> %s is not allowed", e.From, e.To)
}

// NotFoundError for missing / out-of-scope alerts.
type NotFoundError struct{ ID uint64 }

func (e *NotFoundError) Error() string { return fmt.Sprintf("alert %d not found", e.ID) }

// Transition moves an alert to a new state, writes the audit log, and stamps
// the lifecycle timestamps. Terminal states cannot be changed.
func Transition(db *gorm.DB, alertID uint64, target, note string, actor *models.APIKey, now time.Time) (*models.Alert, error) {
	if _, ok := allowedTransitions[target]; !ok {
		return nil, fmt.Errorf("unknown target status %q", target)
	}
	var out *models.Alert
	err := db.Transaction(func(tx *gorm.DB) error {
		var a models.Alert
		if err := tx.First(&a, alertID).Error; err != nil {
			if err == gorm.ErrRecordNotFound {
				return &NotFoundError{ID: alertID}
			}
			return err
		}
		if !allowedTransitions[a.Status][target] {
			return &InvalidTransitionError{From: a.Status, To: target}
		}
		// Snapshot the previous status BEFORE GORM mutates the struct. Putting
		// a.Status directly in the detail map aliases the field, so when GORM's
		// Updates writes the new status back into a and the audit row is later
		// marshaled, "from" would wrongly show the new status.
		fromStatus := a.Status

		updates := map[string]any{"status": target, "updated_at": now}
		if fromStatus == models.AlertStatusNew {
			updates["acknowledged_at"] = now
		}
		if target == models.AlertStatusEscalated {
			updates["escalated_at"] = now
		}
		if target == models.AlertStatusResolved || target == models.AlertStatusFalsePositive {
			updates["resolved_at"] = now
		}
		if err := tx.Model(&a).Updates(updates).Error; err != nil {
			return err
		}

		detail := models.JSONMap{
			"from": fromStatus,
			"to":   target,
			"note": note,
		}
		audit := models.AuditLog{
			Action:     detection.AuditAlertTransition,
			EntityType: "alert",
			EntityID:   fmt.Sprintf("%d", a.ID),
			Detail:     detail,
			CreatedAt:  now,
		}
		if actor != nil {
			audit.ActorKeyID = &actor.ID
			audit.ActorName = actor.Name
		}
		if err := tx.Create(&audit).Error; err != nil {
			return err
		}
		a.Status = target
		out = &a
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}
