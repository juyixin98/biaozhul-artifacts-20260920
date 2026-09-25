package buildsvc

import (
	"context"
	"time"
)

// contextWithTimeout is split out so it is easy to find/replace; a trivial
// wrapper over context.WithTimeout.
func contextWithTimeout(parent context.Context, d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, d)
}
