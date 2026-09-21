package ingest

import (
	"testing"

	"desklens/internal/model"
)

func TestClassifyOrdering(t *testing.T) {
	// Rules are presented in the order the store guarantees from SQL
	// (priority ASC, rule_id ASC).
	rules := []model.Rule{
		{RuleID: "a-first", Pattern: "app*", Category: model.CategoryProductive, Priority: 10},
		{RuleID: "b-second", Pattern: "app*", Category: model.CategoryUnproductive, Priority: 10},
		{RuleID: "low-priority-match", Pattern: "app*", Category: model.CategoryNeutral, Priority: 100},
	}
	// Same priority: lower rule_id wins deterministically.
	cat, rule := classify(rules, "Application")
	if cat != model.CategoryProductive || rule != "a-first" {
		t.Errorf("tie-break = (%s,%s), want productive/a-first", cat, rule)
	}

	// No matching rule: neutral with empty rule id.
	cat, rule = classify(rules, "notapp")
	if cat != model.CategoryNeutral || rule != "" {
		t.Errorf("unmatched = (%s,%s), want neutral/no-rule", cat, rule)
	}
}

func TestClassifyNoRulesMeansNeutral(t *testing.T) {
	cat, rule := classify(nil, "anything")
	if cat != model.CategoryNeutral || rule != "" {
		t.Errorf("no rules = (%s,%s)", cat, rule)
	}
}
