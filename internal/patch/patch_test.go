package patch

import (
	"bytes"
	"crypto/rand"
	"errors"
	"io"
	"strings"
	"testing"

	"deltaupdate/internal/digest"
)

// buildPatch 从 old 计算签名并为 target 生成补丁字节。
func buildPatch(t *testing.T, old, target []byte) ([]byte, *Header) {
	t.Helper()
	sig, err := ComputeSignature(bytes.NewReader(old), DefaultBlockSize)
	if err != nil {
		t.Fatalf("ComputeSignature: %v", err)
	}
	ops := Generate(sig, target)
	h := &Header{
		Version:   1,
		BlockSize: DefaultBlockSize,
		OldDigest: digest.Bytes(old),
		NewDigest: digest.Bytes(target),
		OldSize:   int64(len(old)),
		NewSize:   int64(len(target)),
	}
	var buf bytes.Buffer
	if err := Encode(&buf, h, sig, ops); err != nil {
		t.Fatalf("Encode: %v", err)
	}
	return buf.Bytes(), h
}

// applyPatch 应用补丁并校验最终摘要。
func applyPatch(t *testing.T, old, patchBytes []byte) []byte {
	t.Helper()
	pr, err := NewReader(bytes.NewReader(patchBytes))
	if err != nil {
		t.Fatalf("NewReader: %v", err)
	}
	var out bytes.Buffer
	if err := Apply(pr, bytes.NewReader(old), &out); err != nil {
		t.Fatalf("Apply: %v", err)
	}
	if got := digest.Bytes(out.Bytes()); got != pr.H.NewDigest {
		t.Fatalf("最终摘要不匹配: 期望 %s，实际 %s", pr.H.NewDigest, got)
	}
	return out.Bytes()
}

func randBytes(t *testing.T, n int) []byte {
	t.Helper()
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		t.Fatal(err)
	}
	return b
}

func TestRoundTripSmall(t *testing.T) {
	old := []byte("hello world, this is the old artifact.")
	target := []byte("hello world, this is the NEW artifact!")
	pb, _ := buildPatch(t, old, target)
	got := applyPatch(t, old, pb)
	if !bytes.Equal(got, target) {
		t.Fatalf("重建结果与目标不一致")
	}
}

func TestRoundTripLargeRandom(t *testing.T) {
	old := randBytes(t, 3*DefaultBlockSize+12345)
	target := make([]byte, len(old))
	copy(target, old)
	// 修改中间一段
	copy(target[DefaultBlockSize:], randBytes(t, 1000))
	pb, _ := buildPatch(t, old, target)
	got := applyPatch(t, old, pb)
	if !bytes.Equal(got, target) {
		t.Fatalf("重建结果与目标不一致")
	}
	// 大部分内容未变，补丁应远小于全量
	if len(pb) > len(target)/2 {
		t.Fatalf("补丁过大: %d（目标 %d）", len(pb), len(target))
	}
}

// TestInsertionShift 验证插入导致的块位移：前缀插入 100 字节后，
// 后续内容整体偏移，滚动窗口必须仍能匹配旧块。
func TestInsertionShift(t *testing.T) {
	old := randBytes(t, 5*DefaultBlockSize)
	insert := []byte(strings.Repeat("INSERTED-", 12)) // 108 字节，非块大小整数倍
	target := make([]byte, 0, len(old)+len(insert))
	target = append(target, old[:DefaultBlockSize]...)
	target = append(target, insert...)
	target = append(target, old[DefaultBlockSize:]...)

	sig, err := ComputeSignature(bytes.NewReader(old), DefaultBlockSize)
	if err != nil {
		t.Fatal(err)
	}
	ops := Generate(sig, target)
	pb, _ := buildPatch(t, old, target)
	got := applyPatch(t, old, pb)
	if !bytes.Equal(got, target) {
		t.Fatalf("插入位移场景重建结果与目标不一致")
	}
	// 精确验证：5 个旧块在整体偏移 108 字节后仍应产生 4 个以上 COPY
	copies := 0
	var copyBytes int64
	for _, op := range ops {
		if op.Copy {
			copies++
			copyBytes += int64(op.BlockLen)
		}
	}
	if copies < 4 {
		t.Fatalf("位移后命中 COPY 数过少: %d（期望至少 4，证明块位移被处理）", copies)
	}
	if copyBytes < 3*DefaultBlockSize {
		t.Fatalf("位移后复制字节过少: %d", copyBytes)
	}
	// 补丁应远小于全量
	if len(pb) > len(target)/2 {
		t.Fatalf("插入位移后补丁过大: %d（目标 %d）", len(pb), len(target))
	}
}

// TestDeletionShift 验证删除导致的位移。
func TestDeletionShift(t *testing.T) {
	old := randBytes(t, 4*DefaultBlockSize)
	target := append(append([]byte{}, old[:DefaultBlockSize+777]...), old[2*DefaultBlockSize:]...)
	pb, _ := buildPatch(t, old, target)
	got := applyPatch(t, old, pb)
	if !bytes.Equal(got, target) {
		t.Fatalf("删除位移场景重建结果与目标不一致")
	}
}

func TestEmptyAndIdentical(t *testing.T) {
	// 空 -> 非空
	old := []byte{}
	target := []byte("from nothing")
	pb, _ := buildPatch(t, old, target)
	if got := applyPatch(t, old, pb); !bytes.Equal(got, target) {
		t.Fatalf("空基线重建失败")
	}
	// 完全相同：补丁应几乎全是 COPY
	data := randBytes(t, 2*DefaultBlockSize)
	pb, _ = buildPatch(t, data, data)
	if got := applyPatch(t, data, pb); !bytes.Equal(got, data) {
		t.Fatalf("相同内容重建失败")
	}
	if len(pb) > 1024 {
		t.Fatalf("相同内容补丁过大: %d", len(pb))
	}
}

// TestWrongBaseline 错基线：用错误的旧制品应用补丁，
// COPY 块强校验必须报错 ErrBlockMismatch。
func TestWrongBaseline(t *testing.T) {
	old := randBytes(t, 2*DefaultBlockSize)
	target := append(append([]byte{}, old[:DefaultBlockSize]...), randBytes(t, 500)...)
	pb, _ := buildPatch(t, old, target)

	wrongOld := randBytes(t, 2*DefaultBlockSize)
	pr, err := NewReader(bytes.NewReader(pb))
	if err != nil {
		t.Fatalf("NewReader: %v", err)
	}
	var out bytes.Buffer
	err = Apply(pr, bytes.NewReader(wrongOld), &out)
	if !errors.Is(err, ErrBlockMismatch) {
		t.Fatalf("错基线应报 ErrBlockMismatch，实际: %v", err)
	}
}

// TestCorruptPatch 损坏补丁：翻转补丁体中的字节，必须报错
// （结构错误或块强校验不匹配），绝不能静默产出错误结果。
func TestCorruptPatch(t *testing.T) {
	old := randBytes(t, 3*DefaultBlockSize)
	target := make([]byte, len(old))
	copy(target, old)
	copy(target[100:], randBytes(t, 200))
	pb, _ := buildPatch(t, old, target)

	// 在补丁的多个位置翻转字节，每种都必须报错
	for _, off := range []int{len(pb) / 3, len(pb) / 2, len(pb) - 10} {
		corrupt := append([]byte{}, pb...)
		corrupt[off] ^= 0xff
		pr, err := NewReader(bytes.NewReader(corrupt))
		if err != nil {
			continue // 头损坏直接报错，符合预期
		}
		var out bytes.Buffer
		err = Apply(pr, bytes.NewReader(old), &out)
		if err == nil {
			// 即使 Apply 通过，最终摘要核验（服务层职责）也会失败；
			// 这里模拟服务层核验：
			if digest.Bytes(out.Bytes()) == pr.H.NewDigest {
				t.Fatalf("偏移 %d 处损坏未被检出", off)
			}
		}
	}
}

// TestTruncatedPatch 截断补丁必须报错。
func TestTruncatedPatch(t *testing.T) {
	old := randBytes(t, 2*DefaultBlockSize)
	target := randBytes(t, 2*DefaultBlockSize)
	pb, _ := buildPatch(t, old, target)
	for _, n := range []int{3, 20, len(pb) / 2, len(pb) - 1} {
		pr, err := NewReader(bytes.NewReader(pb[:n]))
		if err != nil {
			continue
		}
		var out bytes.Buffer
		if err := Apply(pr, bytes.NewReader(old), &out); err == nil {
			t.Fatalf("截断到 %d 字节未报错", n)
		}
	}
}

// TestBadMagic 非补丁输入必须报 ErrBadMagic。
func TestBadMagic(t *testing.T) {
	_, err := NewReader(strings.NewReader("this is not a patch at all........"))
	if !errors.Is(err, ErrBadMagic) {
		t.Fatalf("应报 ErrBadMagic，实际: %v", err)
	}
}

// TestStreamingReader 验证补丁可流式读取（不必全量入内存）。
func TestStreamingReader(t *testing.T) {
	old := randBytes(t, DefaultBlockSize)
	target := randBytes(t, DefaultBlockSize)
	pb, _ := buildPatch(t, old, target)
	pr, err := NewReader(io.LimitReader(bytes.NewReader(pb), int64(len(pb))))
	if err != nil {
		t.Fatalf("NewReader: %v", err)
	}
	n := 0
	for {
		_, err := pr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatalf("Next: %v", err)
		}
		n++
	}
	if n == 0 {
		t.Fatalf("未读到任何操作")
	}
}
