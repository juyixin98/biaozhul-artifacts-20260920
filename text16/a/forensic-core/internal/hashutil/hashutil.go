// Package hashutil 提供只读分块哈希与文件身份/变更检测。
package hashutil

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"syscall"
)

// ErrChangedDuringRead 表示文件在读取过程中发生了变化。
var ErrChangedDuringRead = errors.New("file changed during read")

// Identity 文件身份指纹：设备号、inode、大小、mtime、ctime。
// 全部字段可比较，可直接用 != 判断变化。
type Identity struct {
	Dev       uint64 `json:"dev"`
	Ino       uint64 `json:"ino"`
	Size      int64  `json:"size"`
	MtimeNsec int64  `json:"mtime_nsec"`
	CtimeNsec int64  `json:"ctime_nsec"`
}

// IdentityOf 通过 fstat 获取已打开文件的身份指纹。
func IdentityOf(f *os.File) (Identity, error) {
	fi, err := f.Stat()
	if err != nil {
		return Identity{}, err
	}
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		return Identity{}, errors.New("hashutil: unsupported platform, syscall.Stat_t unavailable")
	}
	return Identity{
		Dev:       uint64(st.Dev),
		Ino:       st.Ino,
		Size:      st.Size,
		MtimeNsec: st.Mtim.Sec*1_000_000_000 + st.Mtim.Nsec,
		CtimeNsec: st.Ctim.Sec*1_000_000_000 + st.Ctim.Nsec,
	}, nil
}

// CheckUnchanged 重新 fstat 并与之前捕获的身份比较；
// 大小、mtime、ctime 任一变化都视为读取期间被修改。
func CheckUnchanged(f *os.File, id Identity) error {
	cur, err := IdentityOf(f)
	if err != nil {
		return err
	}
	if cur != id {
		return ErrChangedDuringRead
	}
	return nil
}

// ReadChunkAt 从偏移 off 处精确读取 n 字节。文件缩短导致读不满时返回
// ErrChangedDuringRead；其它 I/O 错误原样返回。
func ReadChunkAt(f *os.File, off, n int64) ([]byte, error) {
	buf := make([]byte, n)
	got, err := f.ReadAt(buf, off)
	if err != nil {
		return nil, ErrChangedDuringRead // 读不满说明文件在读取期间被截断/替换
	}
	if int64(got) != n {
		return nil, ErrChangedDuringRead
	}
	return buf, nil
}

// SHA256Hex 计算数据的 SHA-256 十六进制摘要。
func SHA256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// HashWholeFile 以只读方式分块计算整个文件的 SHA-256。
// afterChunk（可为 nil）在每块读完后回调，用于进度汇报与测试注入。
// 读完后重新 fstat，若文件在计算期间发生变化则返回 ErrChangedDuringRead，
// 调用方不得保存该结果作为基线。
func HashWholeFile(f *os.File, chunkSize int64, afterChunk func(done int64)) (digest string, size int64, id Identity, err error) {
	id, err = IdentityOf(f)
	if err != nil {
		return "", 0, Identity{}, err
	}
	h := sha256.New()
	var off int64
	for off < id.Size {
		n := chunkSize
		if rem := id.Size - off; rem < n {
			n = rem
		}
		buf, rerr := ReadChunkAt(f, off, n)
		if rerr != nil {
			return "", 0, Identity{}, rerr
		}
		h.Write(buf)
		off += n
		if afterChunk != nil {
			afterChunk(off)
		}
	}
	if err := CheckUnchanged(f, id); err != nil {
		return "", 0, Identity{}, err
	}
	return fmt.Sprintf("%x", h.Sum(nil)), id.Size, id, nil
}
