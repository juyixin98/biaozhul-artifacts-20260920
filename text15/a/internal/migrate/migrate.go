// Package migrate 提供版本化迁移：每条迁移执行一次并记录在 schema_migrations 表。
package migrate

import (
	"fmt"

	"gorm.io/gorm"

	"proofcycle/internal/models"
)

type migration struct {
	id string
	fn func(tx *gorm.DB) error
}

var migrations = []migration{
	{"0001_init", func(tx *gorm.DB) error {
		return tx.AutoMigrate(
			&models.User{},
			&models.Job{},
			&models.JobReviewer{},
			&models.ChecklistTemplateItem{},
			&models.Revision{},
			&models.ChecklistItem{},
			&models.Comment{},
			&models.ReviewerState{},
			&models.Approval{},
		)
	}},
}

// Run 依次应用未执行过的迁移。
func Run(db *gorm.DB) error {
	if err := db.AutoMigrate(&models.SchemaMigration{}); err != nil {
		return err
	}
	for _, m := range migrations {
		var n int64
		if err := db.Model(&models.SchemaMigration{}).Where("id = ?", m.id).Count(&n).Error; err != nil {
			return err
		}
		if n > 0 {
			continue
		}
		if err := db.Transaction(func(tx *gorm.DB) error {
			if err := m.fn(tx); err != nil {
				return err
			}
			return tx.Create(&models.SchemaMigration{ID: m.id}).Error
		}); err != nil {
			return fmt.Errorf("migration %s: %w", m.id, err)
		}
	}
	return nil
}

// SeedDemo 写入演示用户（幂等）：1 名项目经理、1 名设计师、8 名审查员。
func SeedDemo(db *gorm.DB) error {
	users := []models.User{
		{Name: "alice", Role: models.RolePM},
		{Name: "bob", Role: models.RoleDesigner},
		{Name: "carol", Role: models.RoleReviewer},
		{Name: "dave", Role: models.RoleReviewer},
		{Name: "erin", Role: models.RoleReviewer},
		{Name: "frank", Role: models.RoleReviewer},
		{Name: "grace", Role: models.RoleReviewer},
		{Name: "heidi", Role: models.RoleReviewer},
		{Name: "ivan", Role: models.RoleReviewer},
		{Name: "judy", Role: models.RoleReviewer},
	}
	for _, u := range users {
		if err := db.Where("name = ?", u.Name).FirstOrCreate(&u).Error; err != nil {
			return err
		}
	}
	return nil
}
