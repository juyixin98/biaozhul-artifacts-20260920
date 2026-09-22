package httpapi_test

import (
	"strconv"

	"anomalywatch/internal/detection"
)

func itoa(i uint64) string { return strconv.FormatUint(i, 10) }

// Engine returns the detection engine wired into the harness.
func (h harness) Engine() *detection.Engine { return h.engine }
