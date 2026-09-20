package classify

import "testing"

func TestMatchWildcard(t *testing.T) {
	cases := []struct {
		pattern, s string
		want       bool
	}{
		{"*Code*", "Visual Studio Code", true},
		{"*Code*", "visual studio code", true}, // case-insensitive
		{"Code", "Code", true},
		{"Code", "Code.exe", false},
		{"Slack?", "Slack1", true},
		{"Slack?", "Slack", false},
		{"*", "anything", true},
		{"a.c", "a.c", true}, // regex metachars are literal
		{"a.c", "abc", false},
	}
	for _, c := range cases {
		if got := Match(c.pattern, c.s); got != c.want {
			t.Errorf("Match(%q, %q) = %v, want %v", c.pattern, c.s, got, c.want)
		}
	}
}

func TestClassifyPriorityAndStableTieBreak(t *testing.T) {
	rules := []Rule{
		{ID: 5, Pattern: "*Code*", Category: Neutral, Priority: 10},
		{ID: 2, Pattern: "*Code*", Category: Productive, Priority: 10}, // same priority, lower ID wins
		{ID: 1, Pattern: "*YouTube*", Category: Unproductive, Priority: 1},
		{ID: 3, Pattern: "*", Category: Neutral, Priority: 0},
	}
	if got := Classify(rules, "VS Code"); got != Productive {
		t.Errorf("tie should break by smallest rule ID: got %q", got)
	}
	if got := Classify(rules, "YouTube"); got != Unproductive {
		t.Errorf("got %q", got)
	}
	if got := Classify(rules, "Something Else"); got != Neutral {
		t.Errorf("fallback rule: got %q", got)
	}
}

func TestClassifyDefaultNeutral(t *testing.T) {
	if got := Classify(nil, "Anything"); got != Neutral {
		t.Errorf("no rules should mean neutral, got %q", got)
	}
}
