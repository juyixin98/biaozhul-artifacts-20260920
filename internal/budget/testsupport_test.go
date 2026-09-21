package budget

import (
	"os"
	"testing"

	"gorm.io/gorm"

	"sitevitals/internal/models"
	"sitevitals/internal/store"
)

type gormWrap struct{ db *gorm.DB }

func openTestDB(t *testing.T) *gorm.DB {
	t.Helper()
	dsn := os.Getenv("SV_TEST_DSN")
	if dsn == "" {
		dsn = "root:@tcp(127.0.0.1:3306)/sitevitals_b_budget_test?charset=utf8mb4&parseTime=True&loc=UTC&multiStatements=true"
	}
	db, err := store.Open(dsn)
	if err != nil {
		t.Skipf("mysql not available: %v", err)
	}
	if err := store.AutoMigrate(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	for _, m := range models.AllModels() {
		stmt := &gorm.Statement{DB: db}
		if err := stmt.Parse(m); err == nil {
			_ = db.Exec("DELETE FROM " + stmt.Schema.Table).Error
		}
	}
	t.Cleanup(func() {
		sqlDB, _ := db.DB()
		_ = sqlDB.Close()
	})
	return db
}
