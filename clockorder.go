package main

// ClockOrder is the result of comparing two vector clocks under their
// partial order.
type ClockOrder int

const (
	// ClockEqual: the two clocks describe the same history.
	ClockEqual ClockOrder = iota
	// ClockBefore: the first clock happened-before the second.
	ClockBefore
	// ClockAfter: the second clock happened-before the first.
	ClockAfter
	// ClockConcurrent: the clocks are incomparable, i.e. the events are
	// causally concurrent and must be kept as sibling versions.
	ClockConcurrent
)

func (o ClockOrder) String() string {
	switch o {
	case ClockEqual:
		return "equal"
	case ClockBefore:
		return "before"
	case ClockAfter:
		return "after"
	case ClockConcurrent:
		return "concurrent"
	default:
		return "unknown"
	}
}
