package clock

import "errors"

var (
	ErrRealClock       = errors.New("clock is in real mode and cannot be set manually")
	ErrNegativeAdvance = errors.New("clock cannot be advanced by a negative duration")
)
