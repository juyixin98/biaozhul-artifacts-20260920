package blob_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"atomicpromo/internal/blob"
	"atomicpromo/internal/crypto/sig"
)

func TestPutAndCopyVerify(t *testing.T) {
	root := t.TempDir()
	st, err := blob.New(root)
	if err != nil {
		t.Fatal(err)
	}
	content := bytes.Repeat([]byte("atomic-build-"), 4096)
	digest, size, err := st.PutCanonical(bytes.NewReader(content))
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(content)
	want := sig.DigestPrefix + hex.EncodeToString(sum[:])
	if digest != want || size != int64(len(content)) {
		t.Fatalf("digest/size mismatch: %s %d", digest, size)
	}

	// 真实复制到环境并重新哈希
	verified, err := st.CopyToEnv(context.Background(), "staging", digest)
	if err != nil {
		t.Fatalf("copy: %v", err)
	}
	if verified != digest {
		t.Fatalf("verified %s != digest %s", verified, digest)
	}
	dst, _ := st.EnvPath("staging", digest)
	got, err := os.ReadFile(dst)
	if err != nil || !bytes.Equal(got, content) {
		t.Fatalf("env copy content mismatch")
	}
}

func TestCopyCorruptionDetected(t *testing.T) {
	root := t.TempDir()
	st, _ := blob.New(root)
	content := []byte("the quick brown fox jumps over the lazy dog 0123456789")
	digest, _, _ := st.PutCanonical(bytes.NewReader(content))

	ctx := blob.WithFault(context.Background(), &blob.Fault{CorruptBytes: 10})
	_, err := st.CopyToEnv(ctx, "staging", digest)
	if !errors.Is(err, blob.ErrVerify) {
		t.Fatalf("want ErrVerify, got %v", err)
	}
	// 损坏的临时/目标文件不得残留为环境副本
	dst, _ := st.EnvPath("staging", digest)
	if _, err := os.Stat(dst); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("corrupt blob must not be visible in env dir")
	}
	// tmp 目录不得留垃圾
	entries, _ := os.ReadDir(filepath.Join(root, "tmp"))
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), "copy-") {
			t.Fatalf("temp copy left behind: %s", e.Name())
		}
	}

	// 无故障重试成功（环境副本此前不存在 → 全新复制）
	v, err := st.CopyToEnv(context.Background(), "staging", digest)
	if err != nil || v != digest {
		t.Fatalf("retry failed: %v %s", err, v)
	}
}

func TestCopyIdempotentReVerify(t *testing.T) {
	root := t.TempDir()
	st, _ := blob.New(root)
	content := []byte("immutable-release-9999")
	digest, _, _ := st.PutCanonical(bytes.NewReader(content))

	if _, err := st.CopyToEnv(context.Background(), "prod", digest); err != nil {
		t.Fatal(err)
	}
	// 第二次为幂等 no-op 且再次校验
	v, err := st.CopyToEnv(context.Background(), "prod", digest)
	if err != nil || v != digest {
		t.Fatalf("idempotent copy: %v", err)
	}
}
