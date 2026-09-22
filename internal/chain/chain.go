// Package chain 实现区块的哈希、签名、链式校验与确定性测试链生成。
//
// 所有密码学操作均为真实执行：
//   - 区块哈希 = SHA-256(height || parent_hash || payload_hash)
//   - payload_hash = SHA-256(payload)
//   - 签名 = Ed25519.Sign(签发者私钥, block_hash)
//
// 攻击者可以篡改 payload / parent_hash / signature，但无法在没有私钥的情况下
// 通过哈希链与签名校验。
package chain

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
)

// GenesisParent 是创世块使用的固定父哈希哨兵值（全 0xff）。
var GenesisParent = bytes32Repeat(0xff)

// 确定性测试签发者密钥（seed 公开、仅用于本地测试）。
// 所有诚实桩节点使用该私钥签名；同步器只内置对应的公钥。
var testSeed = bytes32Repeat(0x7a)

// SigningPublicKey 是同步器信任的测试签发者公钥。
var SigningPublicKey ed25519.PublicKey

var signingPrivateKey ed25519.PrivateKey

func init() {
	signingPrivateKey = ed25519.NewKeyFromSeed(testSeed[:])
	SigningPublicKey = signingPrivateKey.Public().(ed25519.PublicKey)
}

// Block 是链上的一个区块（与 proto 表示解耦，核心层不依赖 gRPC）。
type Block struct {
	Height     uint64 `json:"height"`
	ParentHash []byte `json:"parent_hash"`
	Payload    []byte `json:"payload"`
	Signature  []byte `json:"signature"`
}

// PayloadHash 返回 payload 的 SHA-256。
func (b *Block) PayloadHash() []byte {
	h := sha256.Sum256(b.Payload)
	return h[:]
}

// Hash 返回区块承诺哈希 SHA-256(height || parent_hash || payload_hash)。
func (b *Block) Hash() []byte {
	h := sha256.New()
	var buf [8]byte
	binary.BigEndian.PutUint64(buf[:], b.Height)
	h.Write(buf[:])
	h.Write(b.ParentHash)
	ph := b.PayloadHash()
	h.Write(ph)
	return h.Sum(nil)
}

// VerifyCrypto 验证区块自身的密码学承诺：
// payload_hash 一致（由 Hash 重算隐式覆盖）、签名对 Hash() 有效。
func (b *Block) VerifyCrypto() error {
	if len(b.ParentHash) != sha256.Size {
		return fmt.Errorf("height %d: parent_hash 长度非法: %d", b.Height, len(b.ParentHash))
	}
	if len(b.Signature) != ed25519.SignatureSize {
		return fmt.Errorf("height %d: 签名长度非法: %d", b.Height, len(b.Signature))
	}
	if !ed25519.Verify(SigningPublicKey, b.Hash(), b.Signature) {
		return fmt.Errorf("height %d: Ed25519 签名校验失败（payload 被篡改或签名伪造）", b.Height)
	}
	return nil
}

// VerifyLink 验证本区块正确接在 prev 之后。
func VerifyLink(prev, cur *Block) error {
	if err := cur.VerifyCrypto(); err != nil {
		return err
	}
	if cur.Height != prev.Height+1 {
		return fmt.Errorf("height %d: 高度不连续，前驱高度为 %d", cur.Height, prev.Height)
	}
	ph := prev.Hash()
	if !equalBytes(ph, cur.ParentHash) {
		return fmt.Errorf("height %d: 父哈希不匹配，期望 %s 实际 %s",
			cur.Height, hex.EncodeToString(ph), hex.EncodeToString(cur.ParentHash))
	}
	return nil
}

// NewGenesis 创建并签名创世块（高度 0）。
func NewGenesis(payload []byte) *Block {
	b := &Block{Height: 0, ParentHash: append([]byte(nil), GenesisParent...), Payload: append([]byte(nil), payload...)}
	b.Signature = ed25519.Sign(signingPrivateKey, b.Hash())
	return b
}

// Append 在 prev 之后创建并签名一个新区块。
func Append(prev *Block, payload []byte) *Block {
	b := &Block{
		Height:     prev.Height + 1,
		ParentHash: prev.Hash(),
		Payload:    append([]byte(nil), payload...),
	}
	b.Signature = ed25519.Sign(signingPrivateKey, b.Hash())
	return b
}

// GenerateChain 生成 tip+1 个区块（高度 0..tip），payload 由确定性计数器派生。
func GenerateChain(tip uint64) []*Block {
	blocks := make([]*Block, 0, tip+1)
	blocks = append(blocks, NewGenesis(payloadFor(0)))
	for h := uint64(1); h <= tip; h++ {
		blocks = append(blocks, Append(blocks[h-1], payloadFor(h)))
	}
	return blocks
}

// payloadFor 生成确定性、可辨识的区块负载。
func payloadFor(height uint64) []byte {
	return []byte(fmt.Sprintf("block-%d-payload", height))
}

// CorruptPayload 篡改指定高度区块的 payload（使 payload_hash 承诺与签名失效）。
// 返回被篡改区块的副本。
func CorruptPayload(b *Block) *Block {
	c := cloneBlock(b)
	c.Payload = append([]byte(nil), b.Payload...)
	c.Payload = append(c.Payload, []byte("-CORRUPTED")...)
	return c
}

// CorruptParent 篡改指定区块的父哈希（使链断裂），并重新签名为"另一签发者"是做不到的，
// 因此这里只改 parent_hash —— 签名校验会先失败；为了单独模拟"错误父哈希"故障
// （即签名有效但父指针错误），需要用测试私钥对篡改后的区块重新签名。
func CorruptParent(b *Block) *Block {
	c := cloneBlock(b)
	c.ParentHash = bytes32Repeat(0x00)
	// 用真实私钥对错误承诺重新签名：这样密码学自洽，但链式链接错误，
	// 用于精确区分"父哈希错误"与"中段 payload 损坏"两类故障。
	c.Signature = ed25519.Sign(signingPrivateKey, c.Hash())
	return c
}

func cloneBlock(b *Block) *Block {
	return &Block{
		Height:     b.Height,
		ParentHash: append([]byte(nil), b.ParentHash...),
		Payload:    append([]byte(nil), b.Payload...),
		Signature:  append([]byte(nil), b.Signature...),
	}
}

// ---- 夹具（可信链 + 可信样例）的 JSON 序列化 ----

// Fixture 是一份确定性测试夹具。
type Fixture struct {
	ChainTip      uint64        `json:"chain_tip"`       // 诚实链最高高度
	Blocks        []*Block      `json:"blocks"`          // 完整诚实链（0..ChainTip）
	TrustedSample TrustedSample `json:"trusted_sample"`  // 可信样例（同步器的信任锚）
	SigningKeyHex string        `json:"signing_key_hex"` // 仅桩节点使用的私钥（测试公开）
}

// TrustedSample 是带外可信样例：同步器只信任其中给出的检查点哈希。
type TrustedSample struct {
	Checkpoints map[uint64]string `json:"checkpoints"` // 高度 -> 区块哈希 hex
	TipHeight   uint64            `json:"tip_height"`  // 样例声明的可信链尖
}

// SampleForChain 从诚实链抽样生成可信样例。
func SampleForChain(blocks []*Block, checkpointHeights []uint64) TrustedSample {
	s := TrustedSample{Checkpoints: map[uint64]string{}, TipHeight: blocks[len(blocks)-1].Height}
	for _, h := range checkpointHeights {
		if int(h) < len(blocks) {
			s.Checkpoints[h] = hex.EncodeToString(blocks[h].Hash())
		}
	}
	return s
}

// SaveFixture 将夹具写入 JSON 文件。
func SaveFixture(path string, f *Fixture) error {
	data, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}

// LoadFixture 从 JSON 文件读取夹具。
func LoadFixture(path string) (*Fixture, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var f Fixture
	if err := json.Unmarshal(data, &f); err != nil {
		return nil, err
	}
	if len(f.Blocks) != int(f.ChainTip)+1 {
		return nil, fmt.Errorf("夹具损坏: 区块数 %d 与 chain_tip %d 不符", len(f.Blocks), f.ChainTip)
	}
	return &f, nil
}

// ErrCheckpointMismatch 表示本地已验证链与可信样例检查点冲突。
var ErrCheckpointMismatch = errors.New("可信检查点不匹配")

// VerifyCheckpoints 校验给定区块映射与可信样例完全一致。
func VerifyCheckpoints(blocks map[uint64]*Block, sample TrustedSample) error {
	return VerifyCheckpointsUpTo(blocks, sample, ^uint64(0))
}

// VerifyCheckpointsUpTo 只校验高度 <= maxHeight 的检查点。
// 用于同步中途：尚未拉到的检查点不算失败，已到达的检查点必须一致。
func VerifyCheckpointsUpTo(blocks map[uint64]*Block, sample TrustedSample, maxHeight uint64) error {
	for h, wantHex := range sample.Checkpoints {
		if h > maxHeight {
			continue
		}
		b, ok := blocks[h]
		if !ok {
			return fmt.Errorf("%w: 高度 %d 尚未同步", ErrCheckpointMismatch, h)
		}
		got := hex.EncodeToString(b.Hash())
		if got != wantHex {
			return fmt.Errorf("%w: 高度 %d 哈希 %s != 样例 %s", ErrCheckpointMismatch, h, got, wantHex)
		}
	}
	return nil
}

func bytes32Repeat(v byte) []byte {
	b := make([]byte, sha256.Size)
	for i := range b {
		b[i] = v
	}
	return b
}

func equalBytes(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
