package patch

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

// 定义错误以便调用方用 errors.Is 判定。
var (
	// ErrBadMagic 补丁魔数不匹配（不是本格式补丁）。
	ErrBadMagic = errors.New("补丁魔数不匹配")
	// ErrCorrupt 补丁结构损坏（截断、非法标签、越界长度等）。
	ErrCorrupt = errors.New("补丁已损坏")
	// ErrBlockMismatch COPY 操作从旧制品读出的块强校验不匹配：
	// 错基线、旧制品被改动或补丁本身损坏。
	ErrBlockMismatch = errors.New("数据块强校验不匹配（基线错误或数据损坏）")
)

func errf(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrCorrupt, fmt.Sprintf(format, args...))
}

func marshalHeader(h *Header) ([]byte, error) {
	return json.Marshal(h)
}

// Reader 流式读取一份补丁：先读头，再逐条读出操作。
type Reader struct {
	r *bufio.Reader
	// H 是补丁头。
	H *Header
}

// NewReader 从 r 读取并解析补丁头。后续调用 Next 逐条读取操作。
func NewReader(r io.Reader) (*Reader, error) {
	br := bufio.NewReaderSize(r, 64*1024)
	magic := make([]byte, len(magicV1))
	if _, err := io.ReadFull(br, magic); err != nil {
		return nil, errf("读取魔数失败: %v", err)
	}
	if string(magic) != magicV1 {
		return nil, ErrBadMagic
	}
	hlen64, err := binary.ReadUvarint(br)
	if err != nil {
		return nil, errf("读取头长度失败: %v", err)
	}
	if hlen64 == 0 || hlen64 > 4*1024 {
		return nil, errf("头长度非法: %d", hlen64)
	}
	hj := make([]byte, hlen64)
	if _, err := io.ReadFull(br, hj); err != nil {
		return nil, errf("读取头失败: %v", err)
	}
	h := &Header{}
	if err := json.Unmarshal(hj, h); err != nil {
		return nil, errf("解析头失败: %v", err)
	}
	if h.Version != 1 {
		return nil, errf("不支持的补丁版本: %d", h.Version)
	}
	if h.BlockSize <= 0 || h.OldDigest == "" || h.NewDigest == "" {
		return nil, errf("补丁头字段不完整")
	}
	return &Reader{r: br, H: h}, nil
}

// Next 读出下一条操作。流正常结束时返回 io.EOF。
func (p *Reader) Next() (*Op, error) {
	tag, err := p.r.ReadByte()
	if err != nil {
		if errors.Is(err, io.EOF) {
			return nil, io.EOF
		}
		return nil, errf("读取操作标签失败: %v", err)
	}
	length, err := binary.ReadUvarint(p.r)
	if err != nil {
		return nil, errf("读取记录长度失败: %v", err)
	}
	if length > maxOpRecord {
		return nil, errf("记录长度超出上限: %d", length)
	}
	switch tag {
	case tagCopy:
		// 记录体 = 块索引 uvarint + 块长 uvarint + 强校验(32)
		body := make([]byte, length)
		if _, err := io.ReadFull(p.r, body); err != nil {
			return nil, errf("读取 COPY 记录失败: %v", err)
		}
		br := bytes.NewReader(body)
		bi, err := binary.ReadUvarint(br)
		if err != nil {
			return nil, errf("解析 COPY 块索引失败: %v", err)
		}
		bl, err := binary.ReadUvarint(br)
		if err != nil {
			return nil, errf("解析 COPY 块长度失败: %v", err)
		}
		strong := make([]byte, strongLen)
		if _, err := io.ReadFull(br, strong); err != nil {
			return nil, errf("读取 COPY 强校验失败: %v", err)
		}
		if bl == 0 || bl > uint64(p.H.BlockSize) {
			return nil, errf("COPY 块长度非法: %d", bl)
		}
		return &Op{Copy: true, BlockIndex: uint32(bi), BlockLen: uint32(bl), Data: strong}, nil
	case tagLiteral:
		data := make([]byte, length)
		if _, err := io.ReadFull(p.r, data); err != nil {
			return nil, errf("读取 LITERAL 数据失败: %v", err)
		}
		return &Op{Data: data}, nil
	default:
		return nil, errf("未知操作标签: %d", tag)
	}
}
