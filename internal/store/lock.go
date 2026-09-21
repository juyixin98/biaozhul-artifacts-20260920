package store

import "gorm.io/gorm/clause"

// skipLocked returns "FOR UPDATE SKIP LOCKED" (supported by MySQL 8.0+).
func skipLocked() clause.Locking {
	return clause.Locking{Strength: "UPDATE", Options: "SKIP LOCKED"}
}
