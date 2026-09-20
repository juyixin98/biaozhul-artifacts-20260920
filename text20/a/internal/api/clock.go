package api

import "time"

// nowFn is the clock used by the API. It is a variable so tests can pin time
// for offline thresholds and temporary-price boundary checks.
var nowFn = func() time.Time { return time.Now().UTC() }
