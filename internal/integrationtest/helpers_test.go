package integrationtest

import (
	"errors"

	"desklens/internal/ingest"
)

func isConflict(err error) bool {
	var be *ingest.BatchError
	return errors.As(err, &be) && be.Code == "conflicting_idempotency_key"
}

func isValidation(err error) bool {
	var be *ingest.BatchError
	return errors.As(err, &be) && be.Status == 400
}
