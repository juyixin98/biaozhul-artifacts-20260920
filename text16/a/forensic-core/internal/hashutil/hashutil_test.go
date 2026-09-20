package hashutil_test

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"forensiccore/internal/hashutil"
)

func writeFile(t *testing.T, path string, size int, seed byte) {
	t.Helper()
	data := make([]byte, size)
	for i := range data {
		data[i] = seed + byte(i%251)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestHashWholeFile(t *testing.T) {
	p := filepath.Join(t.TempDir(), "img.dd")
	writeFile(t, p, 5000, 7)
	f, err := os.Open(p)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()

	digest, size, id, err := hashutil.HashWholeFile(f, 1024, nil)
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	if size != 5000 || id.Size != 5000 {
		t.Fatalf("size = %d, want 5000", size)
	}
	raw, _ := os.ReadFile(p)
	want := sha256.Sum256(raw)
	if digest != hex.EncodeToString(want[:]) {
		t.Fatalf("digest mismatch")
	}
}

// 读取期间文件被修改（内容+大小变化）必须被检出。
func TestHashWholeFileDetectsChangeDuringRead(t *testing.T) {
	p := filepath.Join(t.TempDir(), "img.dd")
	writeFile(t, p, 4096, 1)
	f, err := os.Open(p)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()

	mutated := false
	_, _, _, err = hashutil.HashWholeFile(f, 1024, func(done int64) {
		if done == 1024 && !mutated {
			mutated = true
			// 在读取过程中改写文件（追加改变大小，确保 mtime/size 变化）。
			mf, merr := os.OpenFile(p, os.O_WRONLY|os.O_APPEND, 0)
			if merr != nil {
				t.Errorf("mutate open: %v", merr)
				return
			}
			mf.Write([]byte("tampered"))
			mf.Close()
		}
	})
	if !errors.Is(err, hashutil.ErrChangedDuringRead) {
		t.Fatalf("err = %v, want ErrChangedDuringRead", err)
	}
	if !mutated {
		t.Fatal("hook never ran")
	}
}

// 内容相同但元数据被触碰（mtime 变化）也应检出。
func TestCheckUnchangedDetectsTouch(t *testing.T) {
	p := filepath.Join(t.TempDir(), "img.dd")
	writeFile(t, p, 100, 3)
	f, err := os.Open(p)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	id, err := hashutil.IdentityOf(f)
	if err != nil {
		t.Fatal(err)
	}
	if err := hashutil.CheckUnchanged(f, id); err != nil {
		t.Fatalf("unchanged file flagged: %v", err)
	}
	// 等待跨越文件系统时间戳粒度（多粒度时间戳内核上可能为数毫秒），
	// 然后原地重写同长度内容：mtime/ctime 必须变化。
	time.Sleep(20 * time.Millisecond)
	if err := os.WriteFile(p, make([]byte, 100), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := hashutil.CheckUnchanged(f, id); !errors.Is(err, hashutil.ErrChangedDuringRead) {
		t.Fatalf("err = %v, want ErrChangedDuringRead", err)
	}
}
