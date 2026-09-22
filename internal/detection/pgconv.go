package detection

import (
	"time"

	"github.com/jackc/pgx/v5/pgtype"
)

// pgTimestamptz converts a time.Time to a nullable pgtype.Timestamptz.
// The zero time maps to SQL NULL (used for alert columns that do not apply,
// e.g. window_start on sensitive alerts).
func pgTimestamptz(t time.Time) pgtype.Timestamptz {
	if t.IsZero() {
		return pgtype.Timestamptz{Valid: false}
	}
	return pgtype.Timestamptz{Time: t.UTC(), Valid: true}
}
