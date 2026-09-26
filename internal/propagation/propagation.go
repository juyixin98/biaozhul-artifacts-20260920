// Package propagation defines how a retry budget travels across process
// boundaries: request headers carry the request ID, absolute deadline, total
// attempt cap and slots already consumed by upstream layers.
//
// Wire format (all values are plain text):
//
//	X-Request-Id:        opaque caller-chosen correlation ID
//	X-Retry-Deadline:    RFC3339Nano absolute instant (absence = no deadline)
//	X-Retry-Max:         total attempts allowed by the root budget
//	X-Retry-Used:        attempts consumed in upstream layers
package propagation

import (
	"errors"
	"net/http"
	"strconv"
	"time"
)

const (
	HeaderRequestID = "X-Request-Id"
	HeaderDeadline  = "X-Retry-Deadline"
	HeaderMax       = "X-Retry-Max"
	HeaderUsed      = "X-Retry-Used"
)

// Values is the parsed view of a propagated budget.
type Values struct {
	RequestID string
	Deadline  time.Time
	Max       int
	Used      int
	HasBudget bool
}

// Remaining reports slots left after subtracting upstream usage.
func (v Values) Remaining() int {
	if !v.HasBudget {
		return 0
	}
	r := v.Max - v.Used
	if r < 0 {
		return 0
	}
	return r
}

// Inject writes the budget headers onto an outbound request.
func Inject(h http.Header, requestID string, deadline time.Time, max, used int) {
	if h == nil {
		return
	}
	if requestID != "" {
		h.Set(HeaderRequestID, requestID)
	}
	if !deadline.IsZero() {
		h.Set(HeaderDeadline, deadline.UTC().Format(time.RFC3339Nano))
	}
	h.Set(HeaderMax, strconv.Itoa(max))
	h.Set(HeaderUsed, strconv.Itoa(used))
}

// Parse reads budget headers from an inbound request. Missing budget headers
// produce HasBudget=false and no error; malformed values do.
func Parse(h http.Header) (Values, error) {
	v := Values{RequestID: h.Get(HeaderRequestID)}

	maxStr := h.Get(HeaderMax)
	if maxStr == "" {
		return v, nil
	}
	max, err := strconv.Atoi(maxStr)
	if err != nil || max < 0 {
		return v, errors.New("propagation: invalid " + HeaderMax + " header")
	}
	v.Max = max
	v.HasBudget = true

	if usedStr := h.Get(HeaderUsed); usedStr != "" {
		used, err := strconv.Atoi(usedStr)
		if err != nil || used < 0 {
			return v, errors.New("propagation: invalid " + HeaderUsed + " header")
		}
		v.Used = used
	}
	if dl := h.Get(HeaderDeadline); dl != "" {
		t, err := time.Parse(time.RFC3339Nano, dl)
		if err != nil {
			return v, errors.New("propagation: invalid " + HeaderDeadline + " header")
		}
		v.Deadline = t
	}
	return v, nil
}
