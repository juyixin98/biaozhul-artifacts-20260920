package classify

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestGlobMatch(t *testing.T) {
	cases := []struct {
		pattern, name string
		want          bool
	}{
		{"slack", "Slack", true},
		{"*slack*", "Slack for Desktop", true},
		{"*code*", "Visual Studio Code", true},
		{"term?nal", "terminal", true},
		{"term?nal", "termnal", false},
		{"*", "anything", true},
		{"game*", "game of life", true},
		{"*game", "video game", true},
		{"abc", "abcd", false},
		{"", "", true},
		{"a*b*c", "aXbYc", true},
		{"a*b*c", "aXbY", false},
	}
	for _, c := range cases {
		assert.Equal(t, c.want, GlobMatch(c.pattern, c.name), "pattern=%q name=%q", c.pattern, c.name)
	}
}

func TestMatchPriorityAndStableTieBreak(t *testing.T) {
	rules := []Rule{
		{ID: 5, Pattern: "*slack*", Category: Unproductive, Priority: 10},
		{ID: 2, Pattern: "slack*", Category: Productive, Priority: 10}, // same priority, lower ID wins
		{ID: 9, Pattern: "*", Category: Neutral, Priority: 0},
	}
	assert.Equal(t, Productive, Match(rules, "Slack"))

	// Higher priority beats lower rule ID.
	rules2 := []Rule{
		{ID: 1, Pattern: "*slack*", Category: Unproductive, Priority: 0},
		{ID: 7, Pattern: "*slack*", Category: Productive, Priority: 5},
	}
	assert.Equal(t, Productive, Match(rules2, "slack"))
}

func TestMatchDefaultNeutral(t *testing.T) {
	rules := []Rule{{ID: 1, Pattern: "*code*", Category: Productive, Priority: 10}}
	assert.Equal(t, Neutral, Match(rules, "Calculator"))
}
