package fixtures

import (
	"crypto/ed25519"
	"time"

	"inbox/internal/crypto"
	"inbox/internal/merkle"
)

// BuiltMessage is a fully-signed cross-chain message together with its
// Merkle proof and the hash of the block that contains it.
type BuiltMessage struct {
	ChainID     string
	ChannelID   string
	Nonce       uint64
	Payload     []byte
	PayloadHash crypto.Hash
	Commitment  crypto.Hash
	Sender      crypto.KeyPair
	SenderSig   []byte
	Block       crypto.Hash
	Proof       merkle.Proof
}

// BuiltBlock is a fully-signed source block.
type BuiltBlock struct {
	ChainID   string
	Height    uint64
	Parent    crypto.Hash
	Block     crypto.Hash
	MsgRoot   crypto.Hash
	Timestamp int64
	Validator crypto.KeyPair
	Sig       []byte
	Messages  []BuiltMessage
}

// ChainBuilder incrementally constructs signed blocks on one chain.
type ChainBuilder struct {
	chain ChainFixture
	tip   crypto.Hash
	next  uint64
	clock int64
}

// NewChainBuilder starts a builder at genesis.
func NewChainBuilder(cf ChainFixture) *ChainBuilder {
	return &ChainBuilder{chain: cf, next: 1, clock: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC).Unix()}
}

// MsgSpec specifies one message to put in a block.
type MsgSpec struct {
	ChannelID string
	Nonce     uint64
	Sender    crypto.KeyPair
	Payload   []byte
}

// BuildBlock signs a block containing the given messages and advances the
// tip. Message order inside the block is exactly the spec order, and each
// message gets a genuine Merkle inclusion proof.
func (b *ChainBuilder) BuildBlock(specs []MsgSpec) BuiltBlock {
	leaves := make([]crypto.Hash, len(specs))
	commitments := make([]crypto.Hash, len(specs))
	for i, sp := range specs {
		ph := crypto.DigestHash(sp.Payload)
		cm := crypto.MessageCommitment(sp.ChannelID, sp.Nonce, ph)
		commitments[i] = cm
		leaves[i] = cm
	}
	root := merkle.Root(leaves)
	height := b.next
	ts := b.clock + int64(height)
	block := crypto.HeaderHash(b.chain.ID, height, b.tip, root, ts)
	sig := crypto.Sign(b.chain.Validator.Priv, block)

	blk := BuiltBlock{
		ChainID: b.chain.ID, Height: height, Parent: b.tip, Block: block,
		MsgRoot: root, Timestamp: ts, Validator: b.chain.Validator, Sig: sig,
	}
	for i, sp := range specs {
		proof, err := merkle.BuildProof(leaves, i)
		if err != nil {
			panic(err)
		}
		blk.Messages = append(blk.Messages, BuiltMessage{
			ChainID: b.chain.ID, ChannelID: sp.ChannelID, Nonce: sp.Nonce,
			Payload: sp.Payload, PayloadHash: crypto.DigestHash(sp.Payload),
			Commitment: commitments[i], Sender: sp.Sender,
			SenderSig: crypto.Sign(sp.Sender.Priv, commitments[i]),
			Block:     block, Proof: proof,
		})
	}
	b.tip = block
	b.next++
	return blk
}

// EmptyBlock signs a block with no messages (still advances the chain).
func (b *ChainBuilder) EmptyBlock() BuiltBlock { return b.BuildBlock(nil) }

// Tip returns the current tip hash.
func (b *ChainBuilder) Tip() crypto.Hash { return b.tip }

// Height returns the last built block height (0 before genesis).
func (b *ChainBuilder) Height() uint64 { return b.next - 1 }

// Revocation signs revocation evidence for a block hash.
func (b *ChainBuilder) Revocation(block crypto.Hash) (ed25519.PublicKey, []byte) {
	ev := crypto.RevocationHash(b.chain.ID, block)
	return b.chain.Validator.Pub, crypto.Sign(b.chain.Validator.Priv, ev)
}

// ForkBuilder creates a builder that forks from a given parent/height,
// reusing the same validator key (used to create equivocating evidence).
type ForkBuilder struct {
	chain  ChainFixture
	parent crypto.Hash
	height uint64
	clock  int64
}

// NewForkBuilder forks at the block with parent at the given height.
func NewForkBuilder(cf ChainFixture, parent crypto.Hash, parentHeight uint64) *ForkBuilder {
	return &ForkBuilder{
		chain: cf, parent: parent, height: parentHeight + 1,
		clock: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC).Unix(),
	}
}

// BuildBlock constructs one signed block on the fork.
func (f *ForkBuilder) BuildBlock(specs []MsgSpec) BuiltBlock {
	leaves := make([]crypto.Hash, len(specs))
	commitments := make([]crypto.Hash, len(specs))
	for i, sp := range specs {
		ph := crypto.DigestHash(sp.Payload)
		cm := crypto.MessageCommitment(sp.ChannelID, sp.Nonce, ph)
		commitments[i] = cm
		leaves[i] = cm
	}
	root := merkle.Root(leaves)
	ts := f.clock + int64(f.height)
	block := crypto.HeaderHash(f.chain.ID, f.height, f.parent, root, ts)
	sig := crypto.Sign(f.chain.Validator.Priv, block)
	blk := BuiltBlock{
		ChainID: f.chain.ID, Height: f.height, Parent: f.parent, Block: block,
		MsgRoot: root, Timestamp: ts, Validator: f.chain.Validator, Sig: sig,
	}
	for i, sp := range specs {
		proof, _ := merkle.BuildProof(leaves, i)
		blk.Messages = append(blk.Messages, BuiltMessage{
			ChainID: f.chain.ID, ChannelID: sp.ChannelID, Nonce: sp.Nonce,
			Payload: sp.Payload, PayloadHash: crypto.DigestHash(sp.Payload),
			Commitment: commitments[i], Sender: sp.Sender,
			SenderSig: crypto.Sign(sp.Sender.Priv, commitments[i]),
			Block:     block, Proof: proof,
		})
	}
	f.parent = block
	f.height++
	return blk
}
