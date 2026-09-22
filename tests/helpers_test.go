package tests

import (
	"encoding/json"
	"time"

	"dams/internal/service/ruleadmin"
)

func actor(label string) ruleadmin.Actor { return ruleadmin.Actor{Label: label} }

func mustRaw(v any) json.RawMessage { b, _ := json.Marshal(v); return b }

func ptr(t time.Time) *time.Time { return &t }

func createFreqRule(i int) ruleadmin.CreateInput {
	return ruleadmin.CreateInput{
		RuleType:    "frequency",
		Params:      mustRaw(map[string]any{"window_seconds": 300, "threshold": 500 + i}),
		EffectiveAt: ptr(time.Unix(0, 0).UTC()),
	}
}
