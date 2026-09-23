package cache

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestPublishRestoreRoundTrip(t *testing.T) {
	cacheDir := t.TempDir()
	work := t.TempDir()
	st, err := New(cacheDir)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(work, "dist"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(work, "dist", "a.txt"), []byte("A"), 0o644); err != nil {
		t.Fatal(err)
	}
	key := "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	meta := &EntryMeta{Key: key, Node: "n1", Outputs: []string{"dist/a.txt"}, CreatedAt: time.Now()}
	if err := st.Publish(key, work, []string{"dist/a.txt"}, meta); err != nil {
		t.Fatal(err)
	}
	ok, _, err := st.Has(key)
	if err != nil || !ok {
		t.Fatalf("Has = %v, err=%v", ok, err)
	}

	// 从工作目录删除输出，再恢复。
	if err := os.RemoveAll(filepath.Join(work, "dist")); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Restore(key, work); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(work, "dist", "a.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != "A" {
		t.Fatalf("restored content = %q", raw)
	}
}

func TestPublishMissingOutputFails(t *testing.T) {
	st, _ := New(t.TempDir())
	work := t.TempDir()
	key := "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
	err := st.Publish(key, work, []string{"nope.txt"}, &EntryMeta{Key: key, Node: "n"})
	if err == nil {
		t.Fatal("publish with missing output should fail")
	}
	// 不应留下条目。
	if ok, _, _ := st.Has(key); ok {
		t.Fatal("failed publish left a cache entry behind")
	}
}

func TestListHasNoTmpDuplicates(t *testing.T) {
	cacheDir := t.TempDir()
	work := t.TempDir()
	st, err := New(cacheDir)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(work, "a.txt"), []byte("A"), 0o644); err != nil {
		t.Fatal(err)
	}
	key := "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
	meta := &EntryMeta{Key: key, Node: "n1", Outputs: []string{"a.txt"}, CreatedAt: time.Now()}
	if err := st.Publish(key, work, []string{"a.txt"}, meta); err != nil {
		t.Fatal(err)
	}
	entries, err := st.List()
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Key != key {
		t.Fatalf("want exactly one published entry, got %d: %+v", len(entries), entries)
	}
	// 临时目录必须已被清理。
	if _, err := os.Stat(filepath.Join(cacheDir, key[:2], key, "tmp")); !os.IsNotExist(err) {
		t.Fatalf("tmp publish dir leaked: %v", err)
	}
}

func TestUnknownKeyMiss(t *testing.T) {
	st, _ := New(t.TempDir())
	ok, _, err := st.Has("aa11223344556677889900112233445566778899001122334455667788990011")
	if err != nil {
		t.Fatal(err)
	}
	if ok {
		t.Fatal("unknown key should miss")
	}
}
