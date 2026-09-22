package detection

import (
	"time"

	"activityguard/internal/models"
	"activityguard/internal/util"

	"gorm.io/datatypes"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

// alertInput 是规则评估产生的待写入告警。
type alertInput struct {
	RuleKey      string
	RuleVersion  int
	EmployeeID   string
	DepartmentID string
	Severity     string
	Title        string
	Summary      string
	DedupKey     string
	Evidence     []byte
	WindowStart  *time.Time
	WindowEnd    *time.Time
	EventTime    time.Time // 该窗口内最后一次相关事件时间
}

// upsertAlert 按 dedup_key 幂等写入告警（补算/重复调度安全）：
//   - 已存在：仅刷新依据、摘要、window、last_event_at，绝不覆盖人工状态；
//   - 不存在：插入 new 告警。
//
// 并发下唯一键冲突时回退为“读取已有 + 更新动态字段”。
func (e *Engine) upsertAlert(tx *gorm.DB, in alertInput, now time.Time) error {
	if len(in.Evidence) == 0 {
		in.Evidence = []byte("{}")
	}
	a := models.Alert{
		ID:           util.NewID(),
		RuleKey:      in.RuleKey,
		RuleVersion:  in.RuleVersion,
		EmployeeID:   in.EmployeeID,
		DepartmentID: in.DepartmentID,
		Severity:     in.Severity,
		Status:       models.AlertNew,
		Title:        in.Title,
		Summary:      in.Summary,
		DedupKey:     in.DedupKey,
		Evidence:     datatypes.JSON(in.Evidence),
		WindowStart:  in.WindowStart,
		WindowEnd:    in.WindowEnd,
		FirstSeenAt:  in.EventTime,
		LastEventAt:  in.EventTime,
	}
	err := tx.Clauses(clause.OnConflict{
		Columns: []clause.Column{{Name: "dedup_key"}},
		DoUpdates: clause.Assignments(map[string]any{
			"evidence":     datatypes.JSON(in.Evidence),
			"summary":      in.Summary,
			"title":        in.Title,
			"window_start": in.WindowStart,
			"window_end":   in.WindowEnd,
			// 重算可能把窗口起点推早（如补录更早的首次 USB）或推后。
			"first_seen_at": gorm.Expr("LEAST(first_seen_at, VALUES(first_seen_at))"),
			"last_event_at": gorm.Expr("GREATEST(last_event_at, VALUES(last_event_at))"),
			"rule_version":  in.RuleVersion,
		}),
	}).Create(&a).Error
	if err == nil {
		return nil
	}
	// 极端情况下回退：先查再更新动态字段。
	var existing models.Alert
	if gerr := tx.Where("dedup_key = ?", in.DedupKey).First(&existing).Error; gerr != nil {
		return err
	}
	return tx.Model(&models.Alert{}).Where("id = ?", existing.ID).Updates(map[string]any{
		"evidence":      datatypes.JSON(in.Evidence),
		"summary":       in.Summary,
		"title":         in.Title,
		"window_start":  in.WindowStart,
		"window_end":    in.WindowEnd,
		"last_event_at": in.EventTime,
		"rule_version":  in.RuleVersion,
	}).Error
}

// autoResolve 把补算后不再成立的统计告警自动标记为 resolved。
// 仅处理仍为 new 的告警（未被调查员触碰）；调查中/已处理的告警永远保留人工结论。
func (e *Engine) autoResolve(tx *gorm.DB, dedupKey string, note string, now time.Time) error {
	var a models.Alert
	err := tx.Where("dedup_key = ? AND status = ?", dedupKey, models.AlertNew).First(&a).Error
	if err == gorm.ErrRecordNotFound {
		return nil // 没有或已被人工处理
	}
	if err != nil {
		return err
	}
	return tx.Model(&models.Alert{}).Where("id = ?", a.ID).Updates(map[string]any{
		"status":      models.AlertResolved,
		"resolved_at": now,
	}).Error
}
