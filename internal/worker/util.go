package worker

import (
	"errors"

	"sitevitals/internal/collector"
)

// asCollectError is errors.As bound to *collector.CollectError so worker.go
// stays free of that import concern at call sites.
func asCollectError(err error, target **collector.CollectError) bool {
	return errors.As(err, target)
}
