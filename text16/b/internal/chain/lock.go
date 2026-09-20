package chain

import (
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

// lockCase 锁定案件行以串行化同一案件上的并发追加。
// MySQL 使用 SELECT ... FOR UPDATE 行锁；SQLite 没有该语法，
// 调用方（store.Open）已将其连接池限制为单连接，写入天然串行。
func lockCase(tx *gorm.DB, caseID uint) *gorm.DB {
	q := tx.Model(&caseRow{})
	if tx.Dialector.Name() == "mysql" {
		q = q.Clauses(clause.Locking{Strength: "UPDATE"})
	}
	return q.Where("id = ?", caseID)
}

// caseRow 仅用于锁定案件行，避免循环依赖 model 包时的查询模型重复定义。
type caseRow struct {
	ID uint `gorm:"primaryKey"`
}

func (caseRow) TableName() string { return "cases" }
