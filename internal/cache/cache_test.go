package cache

import (
	"errors"
	"os"
	"testing"

	"licensejudge/internal/expression"
)

func TestPutGetRoundTrip(t *testing.T) {
	c, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	expr := "MIT AND (Apache-2.0 OR GPL-3.0-only)"
	n, err := expression.Parse(expr)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Put(expr, n); err != nil {
		t.Fatalf("Put 失败: %v", err)
	}
	got, err := c.Get(expr)
	if err != nil {
		t.Fatalf("Get 失败: %v", err)
	}
	if expression.String(got) != expression.String(n) {
		t.Errorf("往返后 AST 不一致: %q vs %q", expression.String(got), expression.String(n))
	}
}

func TestGetMiss(t *testing.T) {
	c, _ := New(t.TempDir())
	if _, err := c.Get("MIT"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("未命中应返回 ErrNotFound, 实际 %v", err)
	}
}

func TestGetRejectsDifferentExpression(t *testing.T) {
	c, _ := New(t.TempDir())
	// 手工写入载荷但篡改 Expression 字段，应被安全忽略。
	n, _ := expression.Parse("MIT")
	if err := c.Put("MIT", n); err != nil {
		t.Fatal(err)
	}
	// 找到缓存文件并改写内容。
	target := c.keyFor("MIT")
	tampered := []byte(`{"version":1,"expression":"HACKED","ast":{"kind":"license","license":"HACKED"}}`)
	if err := os.WriteFile(target, tampered, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Get("MIT"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("表达式不一致的缓存应视为未命中, 实际 %v", err)
	}
}

func TestGetRejectsOldVersion(t *testing.T) {
	c, _ := New(t.TempDir())
	n, _ := expression.Parse("Apache-2.0")
	if err := c.Put("Apache-2.0", n); err != nil {
		t.Fatal(err)
	}
	target := c.keyFor("Apache-2.0")
	old := []byte(`{"version":999,"expression":"Apache-2.0","ast":{"kind":"license","license":"Apache-2.0"}}`)
	if err := os.WriteFile(target, old, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Get("Apache-2.0"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("旧版本缓存应失效, 实际 %v", err)
	}
}

func TestWithNodeRoundTrip(t *testing.T) {
	c, _ := New(t.TempDir())
	expr := "LGPL-2.1-only WITH Classpath-exception-2.0"
	n, _ := expression.Parse(expr)
	if err := c.Put(expr, n); err != nil {
		t.Fatal(err)
	}
	got, err := c.Get(expr)
	if err != nil {
		t.Fatal(err)
	}
	if w, ok := got.(*expression.WithNode); !ok || w.Exception != "Classpath-exception-2.0" {
		t.Fatalf("WITH 节点往返失败: %#v", got)
	}
}

func TestNewRejectsEmptyDir(t *testing.T) {
	if _, err := New(""); err == nil {
		t.Fatal("空缓存目录应报错")
	}
}
