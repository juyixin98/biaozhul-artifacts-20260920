package httpx

import (
	"errors"

	"github.com/jackc/pgx/v5/pgconn"
)

// pgUniqueViolation is the SQLSTATE for a unique_violation.
const pgUniqueViolation = "23505"

// isUniqueViolation reports whether err is a PostgreSQL unique constraint
// failure (works for pgx native queries and database/sql wrappers alike).
func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == pgUniqueViolation
}
