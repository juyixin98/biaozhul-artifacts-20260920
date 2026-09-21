package httpapi

import "time"

func durFromSeconds(secs float64) time.Duration {
	if secs <= 0 {
		secs = 60
	}
	return time.Duration(secs * float64(time.Second))
}
