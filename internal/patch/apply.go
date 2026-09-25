package patch

import (
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
)

// Apply 从 pr 读出补丁操作，以 oldReader（旧制品）为基线，把重建出的
// 新制品字节流写入 w。
//
// 安全性质：
//   - oldReader 全程只读，绝不写回或截断；
//   - 每条 COPY 都用补丁头携带的强校验核验从旧制品读出的块，
//     错基线 / 旧制品被改动 / 补丁损坏都会返回 ErrBlockMismatch；
//   - 输出字节数必须严格等于头中 NewSize，否则返回 ErrCorrupt；
//   - 调用方负责在写完后比对最终摘要与头中 NewDigest（见服务层）。
func Apply(pr *Reader, oldReader io.ReaderAt, w io.Writer) error {
	blockSize := pr.H.BlockSize
	var written int64
	oldBuf := make([]byte, blockSize)
	for {
		op, err := pr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return err
		}
		if op.Copy {
			bl := int64(op.BlockLen)
			off := int64(op.BlockIndex) * int64(blockSize)
			if off < 0 || off >= pr.H.OldSize+int64(blockSize) {
				return errf("COPY 偏移越界: index=%d", op.BlockIndex)
			}
			if bl > int64(blockSize) {
				return errf("COPY 块长超过块大小")
			}
			n, rerr := oldReader.ReadAt(oldBuf[:bl], off)
			if rerr != nil && !errors.Is(rerr, io.EOF) {
				return fmt.Errorf("读取旧制品失败: %w", rerr)
			}
			if int64(n) != bl {
				return errf("旧制品第 %d 块长度不足（期望 %d，实际 %d）", op.BlockIndex, bl, n)
			}
			strong := sha256.Sum256(oldBuf[:bl])
			if string(strong[:]) != string(op.Data) {
				return fmt.Errorf("%w: block=%d", ErrBlockMismatch, op.BlockIndex)
			}
			if _, err := w.Write(oldBuf[:bl]); err != nil {
				return err
			}
			written += bl
		} else {
			if _, err := w.Write(op.Data); err != nil {
				return err
			}
			written += int64(len(op.Data))
		}
		if written > pr.H.NewSize {
			return errf("输出超过头中声明的新制品大小 %d", pr.H.NewSize)
		}
	}
	if written != pr.H.NewSize {
		return errf("输出大小不匹配（期望 %d，实际 %d）", pr.H.NewSize, written)
	}
	return nil
}
