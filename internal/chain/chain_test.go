package chain_test

import (
	"crypto/ed25519"
	"encoding/hex"
	"testing"

	"nodesync/internal/chain"
)

func TestHashDeterministic(t *testing.T) {
	b := chain.GenerateChain(100)
	// 同一条链两次生成必须完全一致（确定性）。
	b2 := chain.GenerateChain(100)
	for i := range b {
		if hex.EncodeToString(b[i].Hash()) != hex.EncodeToString(b2[i].Hash()) {
			t.Fatalf("高度 %d 哈希不确定", i)
		}
	}
	if len(b[0].Hash()) != 32 {
		t.Fatalf("区块哈希应为 32 字节，实际 %d", len(b[0].Hash()))
	}
}

func TestValidChainVerifies(t *testing.T) {
	blocks := chain.GenerateChain(10)
	for _, b := range blocks {
		if err := b.VerifyCrypto(); err != nil {
			t.Fatalf("诚实块高度 %d 校验失败: %v", b.Height, err)
		}
	}
	for i := 1; i < len(blocks); i++ {
		if err := chain.VerifyLink(blocks[i-1], blocks[i]); err != nil {
			t.Fatalf("链接 %d->%d 失败: %v", i-1, i, err)
		}
	}
}

// TestTamperedPayloadRejected：改一个字节 payload，SHA-256 承诺变化导致签名失败。
func TestTamperedPayloadRejected(t *testing.T) {
	blocks := chain.GenerateChain(5)
	bad := chain.CorruptPayload(blocks[3])
	if err := bad.VerifyCrypto(); err == nil {
		t.Fatal("payload 被篡改后签名校验必须失败")
	}
	if err := chain.VerifyLink(blocks[2], bad); err == nil {
		t.Fatal("被篡改块不得通过链接校验")
	}
}

// TestForgedSignatureRejected：攻击者自己生成密钥重签一个不同承诺的块。
// 没有同步器内置公钥对应的私钥，伪造签名必须被拒绝。
func TestForgedSignatureRejected(t *testing.T) {
	blocks := chain.GenerateChain(3)
	bad := chain.CorruptPayload(blocks[1])
	// 用一把"攻击者"密钥签名（其公钥不在同步器信任锚中）。
	pub, priv, _ := ed25519.GenerateKey(nil)
	_ = pub
	bad.Signature = ed25519.Sign(priv, bad.Hash())
	if err := bad.VerifyCrypto(); err == nil {
		t.Fatal("攻击者密钥的签名必须被内置公钥拒绝")
	}
}

// TestWrongParentHasValidCryptoButBrokenLink：错误父哈希故障用真实私钥重签，
// 因此自身密码学有效，但链接校验必须失败 —— 两类故障必须可区分。
func TestWrongParentHasValidCryptoButBrokenLink(t *testing.T) {
	blocks := chain.GenerateChain(5)
	bad := chain.CorruptParent(blocks[3])
	if err := bad.VerifyCrypto(); err != nil {
		t.Fatalf("错误父哈希块用真实私钥重签后自身密码学应有效: %v", err)
	}
	if err := chain.VerifyLink(blocks[2], bad); err == nil {
		t.Fatal("父指针错误必须在链接校验阶段被拦截")
	}
}

func TestTrustedSampleMismatch(t *testing.T) {
	blocks := chain.GenerateChain(10)
	sample := chain.SampleForChain(blocks, []uint64{0, 5, 10})
	m := map[uint64]*chain.Block{}
	for _, b := range blocks {
		m[b.Height] = b
	}
	if err := chain.VerifyCheckpoints(m, sample); err != nil {
		t.Fatalf("诚实链应通过检查点: %v", err)
	}
	bad := sample
	bad.Checkpoints[5] = "00"
	if err := chain.VerifyCheckpoints(m, bad); err == nil {
		t.Fatal("被污染的检查点哈希必须被拒绝")
	}
}
