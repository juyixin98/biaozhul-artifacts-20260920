package alerts

import "testing"

// The transition table itself is pure policy; assert its key properties
// without requiring a database.
func TestTransitionTable(t *testing.T) {
	terminal := map[string]bool{
		"resolved":       true,
		"false_positive": true,
	}
	for status, next := range allowedTransitions {
		if terminal[status] && len(next) != 0 {
			t.Fatalf("terminal status %s must have no outgoing transitions", status)
		}
	}
	if !allowedTransitions["new"]["escalated"] {
		t.Fatal("new -> escalated must be allowed")
	}
	if !allowedTransitions["new"]["false_positive"] {
		t.Fatal("new -> false_positive must be allowed")
	}
	if allowedTransitions["resolved"]["investigating"] {
		t.Fatal("resolved must not be reopenable")
	}
}
