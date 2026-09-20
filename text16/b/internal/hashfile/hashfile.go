// Package hashfile 以只读、分块方式对原始镜像计算 SHA-256。
//
// 登记基线采用“两遍读取 + 读前/读后 fstat”策略：
//   - 打开文件时取得身份快照（dev/ino/size/mtime）；
//   - 第一遍分块 SHA-256，读后 fstat 必须与打开时一致；
//   - 第二遍重新完整读取再算一次摘要，必须与第一遍完全相同；
//
// 这样在计算期间文件被追加、截断或原地改写（mtime/size 变化或摘要不同）
// 都会被识别，登记整体失败，绝不保存错误基线。
package hashfile

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"

	"golang.org/x/sys/unix"

	"github.com/example/forensiccore/internal/safeopen"
)

// ErrFileChanged 表示读取/计算期间文件发生变化。
var ErrFileChanged = errors.New("file changed while reading; registration refused")

// Identity 是文件身份快照。恢复复核时必须核对 dev+ino+size，
// 不能只凭文件名或大小判断。
type Identity struct {
	Dev   uint64
	Ino   uint64
	Size  int64
	Mtime int64 // 纳秒
	Nlink uint64
}

// Fstat 返回已打开文件描述符的身份快照。
func Fstat(fd int) (*Identity, error) {
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		return nil, err
	}
	return identityFrom(&st), nil
}

func identityFrom(st *unix.Stat_t) *Identity {
	return &Identity{
		Dev:   st.Dev,
		Ino:   st.Ino,
		Size:  st.Size,
		Mtime: st.Mtim.Nano(),
		Nlink: uint64(st.Nlink),
	}
}

// FromFileStat 由 safeopen 返回的 FileStat 构造 Identity。
func FromFileStat(st *safeopen.FileStat) Identity {
	return Identity{
		Dev:   st.Dev,
		Ino:   st.Ino,
		Size:  st.Size,
		Mtime: st.Mtime,
		Nlink: st.Nlink,
	}
}

// SameIdentity 判断两个身份快照是否指向同一个未变化的文件。
// dev+ino 标识“同一个 inode”，size+mtime 是廉价的变化探针；
// 内容级一致性由块摘要/全量摘要保证。
func SameIdentity(a, b Identity) bool {
	return a.Dev == b.Dev && a.Ino == b.Ino && a.Size == b.Size &&
		a.Mtime == b.Mtime && a.Nlink == b.Nlink
}

// ChunkObserver 在每读完一块时回调，供测试/上层注入“读取中变化”等行为。
// 返回错误会中止哈希。
type ChunkObserver func(offset int64, chunk []byte) error

// ReadChunkAt 从 fd 的 offset 处精确读取 len(buf) 字节。
// 短读（文件在打开后被截断）按 ErrFileChanged 处理。
func ReadChunkAt(fd int, buf []byte, offset int64) error {
	n, err := unix.Pread(fd, buf, offset)
	if err != nil {
		return err
	}
	if n != len(buf) {
		return fmt.Errorf("%w: short read at offset %d, want %d got %d",
			ErrFileChanged, offset, len(buf), n)
	}
	return nil
}

// HashPass 对 [0, wantSize) 范围执行一遍分块 SHA-256，返回大写 hex 摘要。
// 调用前应已通过 fstat 确认 wantSize 与当前文件一致。
func HashPass(fd int, wantSize int64, chunkSize int, observer ChunkObserver) (string, error) {
	if chunkSize <= 0 {
		return "", fmt.Errorf("invalid chunk size %d", chunkSize)
	}
	h := sha256.New()
	buf := make([]byte, chunkSize)
	for offset := int64(0); offset < wantSize; {
		n := int(wantSize - offset)
		if n > chunkSize {
			n = chunkSize
		}
		part := buf[:n]
		if err := ReadChunkAt(fd, part, offset); err != nil {
			return "", err
		}
		h.Write(part)
		if observer != nil {
			if err := observer(offset, part); err != nil {
				return "", err
			}
		}
		offset += int64(n)
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// BaselineResult 是登记基线计算结果。
type BaselineResult struct {
	Size     int64
	SHA256   string // 两遍一致的最终摘要（大写 hex）
	Identity Identity
}

// ComputeBaseline 对已打开的只读文件描述符执行两遍哈希与身份核对。
//
// before 是打开文件时由调用方取得的身份。计算完成后再次 fstat，任何变化
// 都导致 ErrFileChanged；两遍摘要不一致同样失败。
func ComputeBaseline(fd int, before Identity, chunkSize int,
	observer ChunkObserver) (*BaselineResult, error) {

	first, err := HashPass(fd, before.Size, chunkSize, observer)
	if err != nil {
		return nil, err
	}
	after, err := Fstat(fd)
	if err != nil {
		return nil, err
	}
	if !SameIdentity(before, *after) {
		return nil, fmt.Errorf("%w: identity changed after first pass (before=%+v after=%+v)",
			ErrFileChanged, before, after)
	}

	// 第二遍：不触发 observer（注入行为只作用于第一遍），用于捕获第一遍期间
	// 发生但 mtime/size 未在检查点暴露的改写。
	second, err := HashPass(fd, before.Size, chunkSize, nil)
	if err != nil {
		return nil, err
	}
	if first != second {
		return nil, fmt.Errorf("%w: two-pass digests differ (%s vs %s)",
			ErrFileChanged, first, second)
	}
	final, err := Fstat(fd)
	if err != nil {
		return nil, err
	}
	if !SameIdentity(before, *final) {
		return nil, fmt.Errorf("%w: identity changed after second pass", ErrFileChanged)
	}
	return &BaselineResult{Size: before.Size, SHA256: first, Identity: *final}, nil
}

// HashBytes 返回数据块的大写 hex SHA-256。
func HashBytes(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
