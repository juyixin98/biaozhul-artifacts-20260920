package api

import (
	"errors"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// pgInt8 is a short alias for a nullable int8 parameter.
type pgInt8 = pgtype.Int8

// pgDate is a short alias for a nullable date parameter/value.
type pgDate = pgtype.Date

const pgUniqueViolation = "23505"
const pgExclusionViolation = "23P01"

func pgErrCode(err error) (string, bool) {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code, true
	}
	return "", false
}

func isUniqueViolation(err error) bool {
	code, ok := pgErrCode(err)
	return ok && code == pgUniqueViolation
}

func isExclusionViolation(err error) bool {
	code, ok := pgErrCode(err)
	return ok && code == pgExclusionViolation
}
