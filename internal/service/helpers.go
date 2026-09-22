package service

import (
	"strings"

	"gorm.io/gorm/clause"
)

// lockOrgClause returns SELECT ... FOR UPDATE on the organization row. It is
// the serialization point: every transaction that either writes point
// assignments or flips the effective version set for an org takes this lock,
// so the atomic-switch transaction and concurrent point writes cannot
// interleave within one organization.
func lockOrgClause() clause.Expression {
	return clause.Locking{Strength: "UPDATE"}
}

// isDuplicateKey reports whether the error is a MySQL 1062 duplicate entry.
func isDuplicateKey(err error) bool {
	if err == nil {
		return false
	}
	return strings.Contains(err.Error(), "Error 1062") || strings.Contains(err.Error(), "Duplicate entry")
}
