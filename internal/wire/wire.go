// Package wire contains the JSON DTOs shared by the HTTP API, the fixture
// tool and the tests.
package wire

// HeaderRequest submits a signed source block header.
type HeaderRequest struct {
	ChainID         string `json:"chain_id"`
	Height          uint64 `json:"height"`
	ParentHex       string `json:"parent_hex"`
	MsgRootHex      string `json:"msg_root_hex"`
	Timestamp       int64  `json:"timestamp"`
	ValidatorPubHex string `json:"validator_pub_hex"`
	SignatureHex    string `json:"signature_hex"`
}

// RevocationRequest submits validator-signed evidence revoking the current
// unconfirmed tip of a chain.
type RevocationRequest struct {
	ChainID         string `json:"chain_id"`
	BlockHex        string `json:"block_hex"`
	ValidatorPubHex string `json:"validator_pub_hex"`
	SignatureHex    string `json:"signature_hex"`
}

// OpenChannelRequest opens a delivery channel on a source chain and
// registers the sender public keys allowed to post messages into it.
type OpenChannelRequest struct {
	ChainID      string   `json:"chain_id"`
	ChannelID    string   `json:"channel_id"`
	SenderPubHex []string `json:"sender_pub_hex"`
}

// MessageRequest submits a cross-chain message: a sender-signed commitment,
// a Merkle inclusion proof against the containing block, and that block's
// hash.
type MessageRequest struct {
	ChainID      string         `json:"chain_id"`
	ChannelID    string         `json:"channel_id"`
	Nonce        uint64         `json:"nonce"`
	PayloadHex   string         `json:"payload_hex"`
	SenderPubHex string         `json:"sender_pub_hex"`
	SenderSigHex string         `json:"sender_sig_hex"`
	BlockHex     string         `json:"block_hex"`
	Proof        []ProofStepDTO `json:"proof"`
}

// ProofStepDTO is one level of a Merkle inclusion proof on the wire.
// Side: 0 = sibling on the left, 1 = sibling on the right.
type ProofStepDTO struct {
	SiblingHex string `json:"sibling_hex"`
	Side       int    `json:"side"`
}

// MessageView is the stored state of one message.
type MessageView struct {
	ChainID        string `json:"chain_id"`
	ChannelID      string `json:"channel_id"`
	Nonce          uint64 `json:"nonce"`
	PayloadHashHex string `json:"payload_hash_hex"`
	BlockHex       string `json:"block_hex"`
	Status         string `json:"status"`
}

// ChannelView is the state of one channel.
type ChannelView struct {
	ChainID   string   `json:"chain_id"`
	ChannelID string   `json:"channel_id"`
	NextNonce uint64   `json:"next_nonce"`
	Status    string   `json:"status"`
	Senders   []string `json:"sender_pub_hex"`
}

// HeaderView is a stored source block header.
type HeaderView struct {
	ChainID    string `json:"chain_id"`
	Height     uint64 `json:"height"`
	BlockHex   string `json:"block_hex"`
	ParentHex  string `json:"parent_hex"`
	MsgRootHex string `json:"msg_root_hex"`
	Canonical  bool   `json:"canonical"`
	Status     string `json:"status"` // active | revoked
}

// DeliveryView is one executed delivery record.
type DeliveryView struct {
	ChainID   string `json:"chain_id"`
	ChannelID string `json:"channel_id"`
	Nonce     uint64 `json:"nonce"`
	BlockHex  string `json:"block_hex"`
	Attempts  int    `json:"attempts"`
}

// AlertView is one operator alert.
type AlertView struct {
	ID        int    `json:"id"`
	Severity  string `json:"severity"` // critical | warning | info
	Kind      string `json:"kind"`
	ChainID   string `json:"chain_id"`
	ChannelID string `json:"channel_id"`
	Message   string `json:"message"`
	CreatedAt string `json:"created_at"`
}

// ErrorResponse is the standard error envelope.
type ErrorResponse struct {
	Error string `json:"error"`
	Code  string `json:"code"`
}

// ProcessResult reports how many messages a processing tick delivered.
type ProcessResult struct {
	Delivered int `json:"delivered"`
}
