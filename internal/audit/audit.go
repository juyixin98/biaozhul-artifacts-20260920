// Package audit 提供审计日志写入辅助。
package audit

import (
	"encoding/json"

	"activityguard/internal/authctx"
	"activityguard/internal/models"
	"activityguard/internal/util"

	"gorm.io/datatypes"
	"gorm.io/gorm"
)

// Write 记录一条审计日志。actor 为 nil 时表示系统动作（如定时任务）。
// 优先在调用方事务内写入；这里提供独立写入版本。
func Write(gdb *gorm.DB, actor *authctx.Identity, action, targetType, targetID string, detail any) error {
	raw, err := json.Marshal(detail)
	if err != nil {
		raw = []byte("{}")
	}
	row := models.AuditLog{
		ID:         util.NewID(),
		Action:     action,
		TargetType: targetType,
		TargetID:   targetID,
		Detail:     datatypes.JSON(raw),
	}
	if actor != nil {
		id := actor.UserID
		row.ActorID = &id
		row.ActorName = actor.Username
	} else {
		row.ActorName = "system"
	}
	return gdb.Create(&row).Error
}

// InTx 在给定事务内写审计，供“调查状态变更/规则修改”与业务变更同事务提交。
func InTx(tx *gorm.DB, actor *authctx.Identity, action, targetType, targetID string, detail any) error {
	raw, err := json.Marshal(detail)
	if err != nil {
		raw = []byte("{}")
	}
	row := models.AuditLog{
		ID:         util.NewID(),
		Action:     action,
		TargetType: targetType,
		TargetID:   targetID,
		Detail:     datatypes.JSON(raw),
	}
	if actor != nil {
		id := actor.UserID
		row.ActorID = &id
		row.ActorName = actor.Username
	} else {
		row.ActorName = "system"
	}
	return tx.Create(&row).Error
}
