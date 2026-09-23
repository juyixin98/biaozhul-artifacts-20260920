package httpapi

import "time"

// eventPollInterval is the SSE bridge poll cadence.
const eventPollInterval = 50 * time.Millisecond

func pollAfter() <-chan time.Time {
	return time.After(eventPollInterval)
}
