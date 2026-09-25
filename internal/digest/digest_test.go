package digest

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBytesFileReader(t *testing.T) {
	data := []byte("verify me with sha-256")
	sum := sha256.Sum256(data)
	want := hex.EncodeToString(sum[:])
	if got := Bytes(data); got != want {
		t.Fatalf("Bytes: %s != %s", got, want)
	}
	if got, err := Reader(bytes.NewReader(data)); err != nil || got != want {
		t.Fatalf("Reader: %s %v", got, err)
	}
	p := filepath.Join(t.TempDir(), "f")
	if err := os.WriteFile(p, data, 0o644); err != nil {
		t.Fatal(err)
	}
	if got, err := File(p); err != nil || got != want {
		t.Fatalf("File: %s %v", got, err)
	}
}

func TestTeeReader(t *testing.T) {
	data := []byte(strings.Repeat("streaming-tee-", 1000))
	var sink bytes.Buffer
	tr := NewTeeReader(bytes.NewReader(data), &sink)
	got, err := Reader(tr)
	if err != nil {
		t.Fatal(err)
	}
	if got != Bytes(data) {
		t.Fatalf("Tee 摘要不匹配")
	}
	if !bytes.Equal(sink.Bytes(), data) {
		t.Fatalf("Tee 落盘内容不匹配")
	}
	// w=nil 只算摘要
	tr2 := NewTeeReader(bytes.NewReader(data), nil)
	if got, err := Reader(tr2); err != nil || got != Bytes(data) {
		t.Fatalf("nil-writer Tee: %s %v", got, err)
	}
}
