package accept

import "testing"

// TestAcceptanceMatrix 把验收枚举纳入 go test ./...。
func TestAcceptanceMatrix(t *testing.T) {
	rep := RunAll()
	for _, c := range rep.Cases {
		t.Run(c.ID, func(t *testing.T) {
			if !c.Pass {
				t.Fatalf("%s: %s", c.Name, c.Detail)
			}
			t.Logf("%s — %s", c.Name, c.Detail)
		})
	}
}
