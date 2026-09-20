package service

import "gorm.io/gorm/clause"

// clauseForUpdate serializes every mutating transaction that touches a job
// onto that job's row (SELECT ... FOR UPDATE), so concurrent uploads, opinion
// updates and sign-offs cannot interleave or bypass the validation rules.
func clauseForUpdate() clause.Locking {
	return clause.Locking{Strength: "UPDATE"}
}
