package servicetest

import (
	"encoding/binary"
	"io"
)

// WriteDeterministic 向 w 写入 size 字节可复现的伪随机数据。
// 使用简单 LCG，避免测试依赖外部工具，且两次运行字节完全一致。
func WriteDeterministic(w io.Writer, size int) error {
	buf := make([]byte, 4096)
	var state uint64 = 0x12345678
	written := 0
	for written < size {
		for i := 0; i < len(buf); i += 8 {
			// LCG: x' = 6364136223846793005*x + 1442695040888963407 (mod 2^64)
			state = state*6364136223846793005 + 1442695040888963407
			binary.LittleEndian.PutUint64(buf[i:i+8], state)
		}
		n := len(buf)
		if written+n > size {
			n = size - written
		}
		if _, err := w.Write(buf[:n]); err != nil {
			return err
		}
		written += n
	}
	return nil
}
