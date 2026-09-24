package locksvc

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"fencingdemo/clock"
)

// 基础：新锁发出 1，续租令牌不变，释放后再获取令牌递增。
func TestAcquireRenewRelease(t *testing.T) {
	clk := clock.NewFake(time.Date(2026, 9, 24, 0, 0, 0, 0, time.UTC))
	svc, err := NewService(clk, "")
	if err != nil {
		t.Fatal(err)
	}

	l, err := svc.Acquire("doc", "A", 10*time.Second)
	if err != nil {
		t.Fatalf("first acquire: %v", err)
	}
	if l.Token != 1 {
		t.Fatalf("first token = %d, want 1", l.Token)
	}

	// 同一持有者续租：令牌保持不变。
	clk.Advance(2 * time.Second)
	l2, err := svc.Acquire("doc", "A", 10*time.Second)
	if err != nil {
		t.Fatalf("renew: %v", err)
	}
	if l2.Token != 1 {
		t.Fatalf("renew token = %d, want 1", l2.Token)
	}
	if !l2.ExpiresAt.Equal(l.ExpiresAt.Add(2 * time.Second)) {
		t.Fatalf("renew expiry not extended: %v vs %v", l2.ExpiresAt, l.ExpiresAt.Add(2*time.Second))
	}

	// 其他人在租约有效时获取：冲突。
	if _, err := svc.Acquire("doc", "B", 10*time.Second); !errors.Is(err, ErrConflict) {
		t.Fatalf("conflict acquire err = %v, want ErrConflict", err)
	}

	if err := svc.Release("doc", "A"); err != nil {
		t.Fatalf("release: %v", err)
	}
	l3, err := svc.Acquire("doc", "B", 10*time.Second)
	if err != nil {
		t.Fatalf("acquire after release: %v", err)
	}
	if l3.Token != 2 {
		t.Fatalf("token after release = %d, want 2", l3.Token)
	}
}

// 租约过期后新持有者获取，得到递增令牌；旧租约查询消失。
func TestExpiryIssuesNewFencingToken(t *testing.T) {
	clk := clock.NewFake(time.Date(2026, 9, 24, 0, 0, 0, 0, time.UTC))
	svc, _ := NewService(clk, "")

	lA, err := svc.Acquire("doc", "A", 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if lA.Token != 1 {
		t.Fatalf("A token = %d, want 1", lA.Token)
	}

	// 旧持有者“暂停”（不续租），时钟推进到租约过期之后。
	clk.Advance(5*time.Second + time.Millisecond)

	if _, ok := svc.Lease("doc"); ok {
		t.Fatal("lease should be expired and invisible")
	}
	lB, err := svc.Acquire("doc", "B", 5*time.Second)
	if err != nil {
		t.Fatalf("B acquire after expiry: %v", err)
	}
	if lB.Token != 2 {
		t.Fatalf("B token = %d, want 2", lB.Token)
	}
	if svc.Counter() != 2 {
		t.Fatalf("counter = %d, want 2", svc.Counter())
	}
}

// 非持有者不能释放别人的锁；过期后也不能释放。
func TestReleaseGuards(t *testing.T) {
	clk := clock.NewFake(time.Date(2026, 9, 24, 0, 0, 0, 0, time.UTC))
	svc, _ := NewService(clk, "")
	if _, err := svc.Acquire("doc", "A", time.Second); err != nil {
		t.Fatal(err)
	}
	if err := svc.Release("doc", "B"); !errors.Is(err, ErrNotHeld) {
		t.Fatalf("foreign release err = %v, want ErrNotHeld", err)
	}
	clk.Advance(2 * time.Second)
	if err := svc.Release("doc", "A"); !errors.Is(err, ErrNotHeld) {
		t.Fatalf("expired release err = %v, want ErrNotHeld", err)
	}
}

func TestInvalidTTL(t *testing.T) {
	svc, _ := NewService(clock.NewFake(time.Now()), "")
	if _, err := svc.Acquire("doc", "A", 0); !errors.Is(err, ErrInvalidTTL) {
		t.Fatalf("ttl=0 err = %v, want ErrInvalidTTL", err)
	}
}

// 重启模拟：用同一状态目录重建服务，计数器继续递增，不回退。
func TestCounterPersistsAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	clk := clock.NewFake(time.Date(2026, 9, 24, 0, 0, 0, 0, time.UTC))

	s1, err := NewService(clk, dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s1.Acquire("doc", "A", time.Second); err != nil {
		t.Fatal(err)
	}
	if err := s1.Release("doc", "A"); err != nil {
		t.Fatal(err)
	}
	if _, err := s1.Acquire("doc", "B", time.Second); err != nil {
		t.Fatal(err)
	}

	// 状态文件确实存在。
	if _, err := os.Stat(filepath.Join(dir, "locksvc.json")); err != nil {
		t.Fatalf("state file missing: %v", err)
	}

	// “重启”：全新实例，同一目录。
	s2, err := NewService(clk, dir)
	if err != nil {
		t.Fatal(err)
	}
	if s2.Counter() != 2 {
		t.Fatalf("counter after restart = %d, want 2", s2.Counter())
	}
	clk.Advance(2 * time.Second) // 让 B 的旧租约过期
	l, err := s2.Acquire("doc", "C", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if l.Token != 3 {
		t.Fatalf("token after restart = %d, want 3 (must not regress)", l.Token)
	}
}
