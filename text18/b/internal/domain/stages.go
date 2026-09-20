package domain

// Incident stages in lifecycle order. A case moves through exactly these and
// may never skip one.
const (
	StageDetected   = "detected"
	StageTriaged    = "triaged"
	StageContained  = "contained"
	StageEradicated = "eradicated"
	StageRecovered  = "recovered"
	StagePostmortem = "postmortem"
	StageClosed     = "closed"
)

// OrderedStages lists every stage in lifecycle order (index == ordinal).
var OrderedStages = []string{
	StageDetected,
	StageTriaged,
	StageContained,
	StageEradicated,
	StageRecovered,
	StagePostmortem,
	StageClosed,
}

var stageOrdinal = func() map[string]int {
	m := make(map[string]int, len(OrderedStages))
	for i, s := range OrderedStages {
		m[s] = i
	}
	return m
}()

// NextStage returns the single stage a case in `from` is allowed to enter,
// or "" when the case is already closed.
func NextStage(from string) string {
	idx, ok := stageOrdinal[from]
	if !ok || idx == len(OrderedStages)-1 {
		return ""
	}
	return OrderedStages[idx+1]
}

func IsValidStage(s string) bool {
	_, ok := stageOrdinal[s]
	return ok
}
