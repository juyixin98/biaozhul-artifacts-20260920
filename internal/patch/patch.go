// Package patch 实现块级差量：对旧制品计算分块签名，
// 据此为新制品生成差量补丁，并把补丁应用到旧制品上重建新制品。
//
// 算法为 rsync 风格：固定块大小，每块计算弱校验（类 Adler-32，可滚动）
// 与强校验（SHA-256）。生成差量时在新制品上滑动窗口，先比弱校验再比强校验，
// 命中则产生 COPY 操作，未命中字节累积为 LITERAL 操作。插入/删除导致的
// 块位移由滚动窗口天然覆盖。
package patch

import (
	"crypto/sha256"
	"encoding/binary"
	"io"
)

// DefaultBlockSize 是默认分块大小（64 KiB）。
const DefaultBlockSize = 64 * 1024

// weak 滚动校验（类 Adler-32）：a 为字节和，b 为加权和。
// 用两个 uint16 拼接成 uint32，滚动更新为 O(1)。
type weak struct{ a, b uint32 }

// weakInit 计算 buf 的初始弱校验。n 为块长度（最后一块可能短于块大小）。
func weakInit(buf []byte, n int) weak {
	var a, b uint32
	for i := 0; i < n; i++ {
		a += uint32(buf[i])
		b += uint32(n-i) * uint32(buf[i])
	}
	return weak{a: a & 0xffff, b: b & 0xffff}
}

// roll 把窗口从 [out, ...in] 滚动一个字节：移出 out，移入 in，窗口长度 n 不变。
func (w weak) roll(out, in byte, n int) weak {
	a := (w.a - uint32(out) + uint32(in)) & 0xffff
	b := (w.b - uint32(n)*uint32(out) + a) & 0xffff
	return weak{a: a, b: b}
}

// sum 返回 32 位弱校验值。
func (w weak) sum() uint32 { return w.b<<16 | w.a }

// BlockSig 是单个数据块的签名。
type BlockSig struct {
	Index  uint32 // 块序号（从 0 开始）
	Weak   uint32 // 弱校验（可滚动）
	Strong []byte // 强校验（SHA-256，32 字节）
}

// Signature 是旧制品的分块签名。
type Signature struct {
	BlockSize int
	Blocks    []BlockSig
}

// ComputeSignature 流式计算 r 的分块签名。
func ComputeSignature(r io.Reader, blockSize int) (*Signature, error) {
	if blockSize <= 0 {
		blockSize = DefaultBlockSize
	}
	sig := &Signature{BlockSize: blockSize}
	buf := make([]byte, blockSize)
	var idx uint32
	for {
		n, err := io.ReadFull(r, buf)
		if n > 0 {
			strong := sha256.Sum256(buf[:n])
			sig.Blocks = append(sig.Blocks, BlockSig{
				Index:  idx,
				Weak:   weakInit(buf, n).sum(),
				Strong: strong[:],
			})
			idx++
		}
		if err == io.EOF || err == io.ErrUnexpectedEOF {
			break
		}
		if err != nil {
			return nil, err
		}
	}
	return sig, nil
}

// Op 是补丁中的一条操作：从旧制品复制一块，或写入一段新数据。
type Op struct {
	// Copy 为 true 时表示从旧制品第 BlockIndex 块复制 BlockLen 字节；
	// 为 false 时表示 LITERAL，Data 为新数据。
	Copy       bool
	BlockIndex uint32
	BlockLen   uint32
	Data       []byte
}

// Generate 根据旧制品签名 sig，为新制品数据 target 生成差量操作序列。
// target 一次性读入内存（README 中记录了该取舍）。
func Generate(sig *Signature, target []byte) []Op {
	blockSize := sig.BlockSize
	if blockSize <= 0 {
		blockSize = DefaultBlockSize
	}
	// 弱校验 -> 块索引列表（弱校验可能碰撞，需再比强校验）。
	byWeak := make(map[uint32][]int, len(sig.Blocks))
	for i, b := range sig.Blocks {
		byWeak[b.Weak] = append(byWeak[b.Weak], i)
	}

	var ops []Op
	litStart := 0 // 当前未落盘的 literal 区间 [litStart, pos)
	flushLiteral := func(upto int) {
		if upto > litStart {
			ops = append(ops, Op{Data: target[litStart:upto]})
		}
	}

	pos := 0
	var w weak
	haveWeak := false
	for pos < len(target) {
		n := blockSize
		if len(target)-pos < n {
			n = len(target) - pos
		}
		if !haveWeak {
			w = weakInit(target[pos:], n)
			haveWeak = true
		}
		matched := false
		if cands, ok := byWeak[w.sum()]; ok {
			strong := sha256.Sum256(target[pos : pos+n])
			for _, bi := range cands {
				bs := sig.Blocks[bi]
				if len(bs.Strong) == len(strong) && string(bs.Strong) == string(strong[:]) {
					// 命中：先落盘之前的 literal，再追加 COPY。
					flushLiteral(pos)
					ops = append(ops, Op{Copy: true, BlockIndex: bs.Index, BlockLen: uint32(n)})
					pos += n
					litStart = pos
					haveWeak = false
					matched = true
					break
				}
			}
		}
		if !matched {
			// 窗口右移一个字节，滚动更新弱校验。
			if pos+blockSize < len(target) {
				w = w.roll(target[pos], target[pos+blockSize], blockSize)
			} else {
				haveWeak = false // 尾部短块，下次重新计算
			}
			pos++
		}
	}
	flushLiteral(len(target))
	return ops
}

// ---- 补丁二进制格式（Patch v1）----
//
// 魔数 8 字节 | 头长度 uvarint | 头 JSON | 操作记录序列
// 操作记录：tag(1 字节) | 长度 uvarint | 数据
//   tag=1 COPY：    块索引 uvarint | 块长 uvarint | 该块 SHA-256(32 字节)
//   tag=2 LITERAL： 数据字节（长度即数据长度）

const (
	magicV1     = "DUPDIFF1"
	tagCopy     = 1
	tagLiteral  = 2
	strongLen   = sha256.Size
	maxOpRecord = 1 << 30 // 单条操作记录长度上限，防御损坏输入
)

// Header 是补丁的 JSON 头，绑定旧/新制品摘要。
type Header struct {
	Version     int    `json:"version"`
	BlockSize   int    `json:"block_size"`
	OldDigest   string `json:"old_digest"` // 基线（旧）制品 SHA-256
	NewDigest   string `json:"new_digest"` // 目标（新）制品 SHA-256
	OldSize     int64  `json:"old_size"`
	NewSize     int64  `json:"new_size"`
	CreatedUnix int64  `json:"created_unix"`
}

// Encode 把补丁写入 w：头 + 操作序列。ops 中 COPY 操作需带块强校验，
// 由 Encode 根据 sig 补全写入。
func Encode(w io.Writer, h *Header, sig *Signature, ops []Op) error {
	if _, err := io.WriteString(w, magicV1); err != nil {
		return err
	}
	hj, err := marshalHeader(h)
	if err != nil {
		return err
	}
	if err := writeUvarint(w, uint64(len(hj))); err != nil {
		return err
	}
	if _, err := w.Write(hj); err != nil {
		return err
	}
	var tmp [binary.MaxVarintLen64 + strongLen]byte
	for _, op := range ops {
		if op.Copy {
			if int(op.BlockIndex) >= len(sig.Blocks) {
				return errf("COPY 块索引越界: %d", op.BlockIndex)
			}
			// 记录体 = 块索引 uvarint + 块长 uvarint + 强校验(32)
			n1 := binary.PutUvarint(tmp[:], uint64(op.BlockIndex))
			n2 := binary.PutUvarint(tmp[n1:], uint64(op.BlockLen))
			if _, err := w.Write([]byte{tagCopy}); err != nil {
				return err
			}
			if err := writeUvarint(w, uint64(n1+n2+strongLen)); err != nil {
				return err
			}
			if _, err := w.Write(tmp[:n1+n2]); err != nil {
				return err
			}
			if _, err := w.Write(sig.Blocks[op.BlockIndex].Strong); err != nil {
				return err
			}
		} else {
			if _, err := w.Write([]byte{tagLiteral}); err != nil {
				return err
			}
			if err := writeUvarint(w, uint64(len(op.Data))); err != nil {
				return err
			}
			if _, err := w.Write(op.Data); err != nil {
				return err
			}
		}
	}
	return nil
}

func writeUvarint(w io.Writer, v uint64) error {
	var tmp [binary.MaxVarintLen64]byte
	n := binary.PutUvarint(tmp[:], v)
	_, err := w.Write(tmp[:n])
	return err
}
