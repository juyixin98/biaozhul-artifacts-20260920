package resourcesvc

import (
	"errors"
	"path/filepath"
	"testing"

	"os"
)

// 核心围栏语义：只接受不低于已见令牌的写。
func TestWriteFencing(t *testing.T) {
	svc, _ := NewService("")

	if err := svc.Write("k", "v2", 2); err != nil {
		t.Fatalf("write token 2: %v", err)
	}
	// 旧持有者迟到的写：拒绝。
	if err := svc.Write("k", "stale", 1); !errors.Is(err, ErrStaleToken) {
		t.Fatalf("write token 1 err = %v, want ErrStaleToken", err)
	}
	// 同令牌重试允许（幂等覆盖）。
	if err := svc.Write("k", "v2-retry", 2); err != nil {
		t.Fatalf("write token 2 retry: %v", err)
	}
	// 更高令牌接受。
	if err := svc.Write("k", "v3", 3); err != nil {
		t.Fatalf("write token 3: %v", err)
	}
	r, _ := svc.Read("k")
	if r.Value != "v3" || r.Token != 3 {
		t.Fatalf("final resource = %+v, want value=v3 token=3", r)
	}
	// 不同资源各自记录已见令牌，互不影响。
	if err := svc.Write("other", "x", 1); err != nil {
		t.Fatalf("write other token 1: %v", err)
	}
}

func TestWriteRequiresToken(t *testing.T) {
	svc, _ := NewService("")
	if err := svc.Write("k", "v", 0); err == nil {
		t.Fatal("token=0 should be rejected")
	}
}

// 重启模拟：已见令牌落盘，重启后旧令牌仍被拒绝。
func TestSeenTokenPersistsAcrossRestart(t *testing.T) {
	dir := t.TempDir()

	s1, err := NewService(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := s1.Write("k", "v3", 3); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "resourcesvc.json")); err != nil {
		t.Fatalf("state file missing: %v", err)
	}

	s2, err := NewService(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := s2.Write("k", "stale-after-restart", 2); !errors.Is(err, ErrStaleToken) {
		t.Fatalf("stale write after restart err = %v, want ErrStaleToken", err)
	}
	r, ok := s2.Read("k")
	if !ok || r.Token != 3 || r.Value != "v3" {
		t.Fatalf("resource after restart = %+v ok=%v", r, ok)
	}
}
