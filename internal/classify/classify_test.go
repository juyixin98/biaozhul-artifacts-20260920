package classify

import (
	"testing"

	"desklens/internal/repo"
)

func TestGlobAndStableOrder(t *testing.T) {
	m, err := NewMatcher(repo.Classification{Version: 7, Rules: []repo.ClassificationRule{
		{RuleID: 5, Pattern: "fire*", Category: "non_productive", Priority: 50},
		{RuleID: 2, Pattern: "fire*", Category: "productive", Priority: 50},
		{RuleID: 9, Pattern: "*code", Category: "productive", Priority: 100},
		{RuleID: 1, Pattern: "*", Category: "neutral", Priority: 0},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if m.Version() != 7 {
		t.Fatalf("version = %d", m.Version())
	}
	cases := map[string]string{
		"Firefox":       "productive", // tied priority 50 -> lower rule_id (2)
		"fire":          "productive",
		"VS Code":       "productive", // '*code'
		"unknown thing": "neutral",    // catch-all
	}
	for app, want := range cases {
		if got := m.Match(app); got != want {
			t.Errorf("Match(%q) = %s, want %s", app, got, want)
		}
	}
}

func TestHigherPriorityWins(t *testing.T) {
	m, _ := NewMatcher(repo.Classification{Rules: []repo.ClassificationRule{
		{RuleID: 1, Pattern: "*", Category: "neutral", Priority: 0},
		{RuleID: 2, Pattern: "*slack*", Category: "productive", Priority: 100},
	}})
	if got := m.Match("Slack"); got != Productive {
		t.Fatalf("got %s", got)
	}
}
