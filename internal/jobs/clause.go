package jobs

import "gorm.io/gorm/clause"

// lockUpdate is SELECT ... FOR UPDATE, matching the org lock the point and
// region services take.
func lockUpdate() clause.Expression {
	return clause.Locking{Strength: "UPDATE"}
}
