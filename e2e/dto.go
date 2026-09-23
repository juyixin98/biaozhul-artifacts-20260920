package e2e

import (
	"crypto/ed25519"
	"encoding/hex"

	"inbox/internal/fixtures"
	"inbox/internal/wire"
)

func hx(b []byte) string { return hex.EncodeToString(b) }

func hexBytes(b []byte) string { return hx(b) }

func openDTO(chainID, channelID string, senders ...ed25519.PublicKey) wire.OpenChannelRequest {
	pubs := make([]string, len(senders))
	for i, p := range senders {
		pubs[i] = hx(p)
	}
	return wire.OpenChannelRequest{ChainID: chainID, ChannelID: channelID, SenderPubHex: pubs}
}

func headerDTO(b fixtures.BuiltBlock) wire.HeaderRequest {
	return wire.HeaderRequest{
		ChainID: b.ChainID, Height: b.Height,
		ParentHex:       hx(b.Parent[:]),
		MsgRootHex:      hx(b.MsgRoot[:]),
		Timestamp:       b.Timestamp,
		ValidatorPubHex: hx(b.Validator.Pub),
		SignatureHex:    hx(b.Sig),
	}
}

func messageDTO(m fixtures.BuiltMessage) wire.MessageRequest {
	steps := make([]wire.ProofStepDTO, len(m.Proof.Steps))
	for i, st := range m.Proof.Steps {
		steps[i] = wire.ProofStepDTO{SiblingHex: hx(st.Sibling[:]), Side: int(st.Side)}
	}
	return wire.MessageRequest{
		ChainID:      m.ChainID,
		ChannelID:    m.ChannelID,
		Nonce:        m.Nonce,
		PayloadHex:   hx(m.Payload),
		SenderPubHex: hx(m.Sender.Pub),
		SenderSigHex: hx(m.SenderSig),
		BlockHex:     hx(m.Block[:]),
		Proof:        steps,
	}
}
