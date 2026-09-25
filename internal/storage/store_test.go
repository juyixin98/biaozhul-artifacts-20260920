package storage

import (
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"
)

func TestCASRoundTripAndDedup(t *testing.T) {
	root := t.TempDir()
	s, err := New(filepath.Join(root, "cache"), filepath.Join(root, "state"), filepath.Join(root, "work"))
	if err != nil {
		t.Fatal(err)
	}
	data := []byte("content-addressable storage object")
	d1, n1, err := s.PutBlob(bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	d2, n2, err := s.PutBlob(bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	if d1 != d2 || n1 != n2 || n1 != int64(len(data)) {
		t.Fatalf("相同内容应得到相同摘要: %s/%d vs %s/%d", d1, n1, d2, n2)
	}
	if !s.HasBlob(d1) {
		t.Fatalf("HasBlob 应命中")
	}
	f, fi, err := s.OpenBlob(d1)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("对象路径 %s 权限 %o", fi.Name(), fi.Mode().Perm())
	if fi.Mode().Perm() != 0o444 {
		t.Fatalf("对象权限应为 0444，实际 %o", fi.Mode().Perm())
	}
	got, err := io.ReadAll(f)
	f.Close()
	if err != nil || !bytes.Equal(got, data) {
		t.Fatalf("读回内容不一致: %v", err)
	}
	// 只读对象不得被写（用存储层给出的完整路径）
	absPath, err := s.BlobPath(d1)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(absPath, []byte("x"), 0o444); err == nil {
		t.Fatalf("只读对象竟可写: %s", absPath)
	}
}

func TestNotFoundAndBadDigest(t *testing.T) {
	root := t.TempDir()
	s, _ := New(filepath.Join(root, "cache"), filepath.Join(root, "state"), filepath.Join(root, "work"))
	if _, _, err := s.OpenBlob("deadbeef"); err == nil {
		t.Fatalf("非法摘要应报错")
	}
	if _, _, err := s.OpenBlob("0000000000000000000000000000000000000000000000000000000000000000"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("不存在对象应报 ErrNotFound，实际 %v", err)
	}
	if _, err := s.GetRef("nope"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("不存在引用应报 ErrNotFound，实际 %v", err)
	}
}

func TestRefsAtomicAndList(t *testing.T) {
	root := t.TempDir()
	s, _ := New(filepath.Join(root, "cache"), filepath.Join(root, "state"), filepath.Join(root, "work"))
	if err := s.SetRef("a", "1111111111111111111111111111111111111111111111111111111111111111"); err != nil {
		t.Fatal(err)
	}
	if err := s.SetRef("b", "2222222222222222222222222222222222222222222222222222222222222222"); err != nil {
		t.Fatal(err)
	}
	if err := s.SetRef("../escape", "x"); err == nil {
		t.Fatalf("非法名称必须被拒绝")
	}
	if d, err := s.GetRef("a"); err != nil || d != "1111111111111111111111111111111111111111111111111111111111111111" {
		t.Fatalf("GetRef 错误: %s %v", d, err)
	}
	refs, err := s.ListRefs()
	if err != nil || len(refs) != 2 || refs["a"] == "" || refs["b"] == "" {
		t.Fatalf("ListRefs 错误: %v %v", refs, err)
	}
}

func TestWorkDirIsolationAndCleanup(t *testing.T) {
	root := t.TempDir()
	s, _ := New(filepath.Join(root, "cache"), filepath.Join(root, "state"), filepath.Join(root, "work"))
	w1, err := s.WorkPath("apply")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(w1, "staged"), []byte("partial"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := s.RemoveWork(w1); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(w1); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("暂存目录应已删除")
	}
	// 拒绝清理工作目录之外的路径
	if err := s.RemoveWork(filepath.Join(root, "state")); err == nil {
		t.Fatalf("不得清理工作目录之外的路径")
	}
	// 残留清理
	w2, _ := s.WorkPath("apply")
	_ = os.WriteFile(filepath.Join(w2, "x"), []byte("y"), 0o644)
	if err := s.CleanWork(); err != nil {
		t.Fatal(err)
	}
	if entries, _ := os.ReadDir(s.WorkDir); len(entries) != 0 {
		t.Fatalf("CleanWork 后仍有 %d 项残留", len(entries))
	}
}

func TestCheckSpace(t *testing.T) {
	root := t.TempDir()
	// cap 限制下应判定为不足
	if err := CheckSpace(root, 1_000_000, 100); err == nil {
		t.Fatalf("应判定空间不足")
	} else if !errors.Is(err, ErrNoSpace) {
		t.Fatalf("应报 ErrNoSpace，实际 %v", err)
	}
	// 无 cap 限制（实际磁盘空间充足）应通过
	if err := CheckSpace(root, 1, 0); err != nil {
		t.Fatalf("空间检查失败: %v", err)
	}
}
