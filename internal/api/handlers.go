package api

import (
	"crypto/ed25519"
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"

	"inbox/internal/core"
	"inbox/internal/crypto"
	"inbox/internal/merkle"
	"inbox/internal/wire"
)

func (s *Server) openChannel(w http.ResponseWriter, r *http.Request) {
	chainID := chi.URLParam(r, "chainID")
	var req wire.OpenChannelRequest
	if err := decode(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid JSON: "+err.Error())
		return
	}
	chID := req.ChannelID
	if chID == "" {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "channel_id is required")
		return
	}
	senders := make([]ed25519.PublicKey, 0, len(req.SenderPubHex))
	for _, hx := range req.SenderPubHex {
		b, err := mustHex(hx, ed25519.PublicKeySize)
		if err != nil {
			writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid sender_pub_hex: "+err.Error())
			return
		}
		senders = append(senders, ed25519.PublicKey(b))
	}
	if err := s.ex.OpenChannel(r.Context(), chainID, chID, senders); err != nil {
		failDomain(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]string{
		"chain_id": chainID, "channel_id": chID, "status": "open",
	})
}

func (s *Server) getChannel(w http.ResponseWriter, r *http.Request) {
	cs, err := s.ex.GetChannel(r.Context(), chi.URLParam(r, "chainID"), chi.URLParam(r, "channelID"))
	if err != nil {
		failDomain(w, err)
		return
	}
	v := wire.ChannelView{
		ChainID: cs.ChainID, ChannelID: cs.ChannelID, NextNonce: cs.NextNonce,
		Status: cs.Status, Senders: make([]string, 0, len(cs.Senders)),
	}
	for _, pub := range cs.Senders {
		v.Senders = append(v.Senders, b2hex(pub))
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) submitHeader(w http.ResponseWriter, r *http.Request) {
	var req wire.HeaderRequest
	if err := decode(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid JSON: "+err.Error())
		return
	}
	parent, err := mustHex(req.ParentHex, 32)
	if err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid parent_hex: "+err.Error())
		return
	}
	root, err := mustHex(req.MsgRootHex, 32)
	if err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid msg_root_hex: "+err.Error())
		return
	}
	pub, err := mustHex(req.ValidatorPubHex, ed25519.PublicKeySize)
	if err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid validator_pub_hex: "+err.Error())
		return
	}
	sig, err := mustHex(req.SignatureHex, ed25519.SignatureSize)
	if err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid signature_hex: "+err.Error())
		return
	}
	var pHash, rHash crypto.Hash
	copy(pHash[:], parent)
	copy(rHash[:], root)
	h := core.Header{
		ChainID: req.ChainID, Height: req.Height, Parent: pHash,
		MsgRoot: rHash, Timestamp: req.Timestamp,
	}
	if err := s.ex.SubmitHeader(r.Context(), h, ed25519.PublicKey(pub), sig); err != nil {
		failDomain(w, err)
		return
	}
	block := crypto.HeaderHash(h.ChainID, h.Height, pHash, rHash, h.Timestamp)
	writeJSON(w, http.StatusAccepted, map[string]string{"block_hex": b2hex(block[:])})
}

func (s *Server) submitRevocation(w http.ResponseWriter, r *http.Request) {
	var req wire.RevocationRequest
	if err := decode(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid JSON: "+err.Error())
		return
	}
	block, err := mustHex(req.BlockHex, 32)
	if err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid block_hex: "+err.Error())
		return
	}
	pub, err := mustHex(req.ValidatorPubHex, ed25519.PublicKeySize)
	if err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid validator_pub_hex: "+err.Error())
		return
	}
	sig, err := mustHex(req.SignatureHex, ed25519.SignatureSize)
	if err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid signature_hex: "+err.Error())
		return
	}
	var bHash crypto.Hash
	copy(bHash[:], block)
	rv := core.Revocation{ChainID: req.ChainID, Block: bHash,
		Validator: ed25519.PublicKey(pub), Signature: sig}
	if err := s.ex.SubmitRevocation(r.Context(), rv); err != nil {
		failDomain(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "revoked", "block_hex": b2hex(block)})
}

func (s *Server) submitMessage(w http.ResponseWriter, r *http.Request) {
	var req wire.MessageRequest
	if err := decode(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid JSON: "+err.Error())
		return
	}
	payload, err := mustHex(req.PayloadHex, -1)
	if err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid payload_hex: "+err.Error())
		return
	}
	if len(payload) == 0 {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "payload_hex must not be empty")
		return
	}
	pub, err := mustHex(req.SenderPubHex, ed25519.PublicKeySize)
	if err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid sender_pub_hex: "+err.Error())
		return
	}
	sig, err := mustHex(req.SenderSigHex, ed25519.SignatureSize)
	if err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid sender_sig_hex: "+err.Error())
		return
	}
	block, err := mustHex(req.BlockHex, 32)
	if err != nil {
		writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid block_hex: "+err.Error())
		return
	}
	var payloadHash, blockHash crypto.Hash
	payloadHash = crypto.DigestHash(payload)
	copy(blockHash[:], block)

	var steps []merkle.ProofStep
	for i, st := range req.Proof {
		sib, err := mustHex(st.SiblingHex, 32)
		if err != nil {
			writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "invalid proof step "+strconv.Itoa(i)+": "+err.Error())
			return
		}
		if st.Side != 0 && st.Side != 1 {
			writeErr(w, http.StatusBadRequest, core.CodeBadRequest, "proof side must be 0 or 1")
			return
		}
		var sh crypto.Hash
		copy(sh[:], sib)
		steps = append(steps, merkle.ProofStep{Sibling: sh, Side: merkle.ProofSide(st.Side)})
	}

	m := core.Message{
		ChainID: req.ChainID, ChannelID: req.ChannelID, Nonce: req.Nonce,
		PayloadHash: payloadHash, Block: blockHash,
		SenderPub: ed25519.PublicKey(pub), SenderSig: sig,
	}
	if err := s.ex.SubmitMessage(r.Context(), m, merkle.Proof{Steps: steps}); err != nil {
		failDomain(w, err)
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]string{
		"status": "accepted", "payload_hash_hex": b2hex(payloadHash[:]),
	})
}

func (s *Server) process(w http.ResponseWriter, r *http.Request) {
	n, err := s.ex.ProcessArmed(r.Context())
	if err != nil {
		failDomain(w, err)
		return
	}
	writeJSON(w, http.StatusOK, wire.ProcessResult{Delivered: n})
}

func (s *Server) listMessages(w http.ResponseWriter, r *http.Request) {
	out, err := s.ex.ListMessages(r.Context(), chi.URLParam(r, "chainID"), chi.URLParam(r, "channelID"))
	if err != nil {
		failDomain(w, err)
		return
	}
	if out == nil {
		out = []wire.MessageView{}
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) listDeliveries(w http.ResponseWriter, r *http.Request) {
	out, err := s.ex.ListDeliveries(r.Context(), chi.URLParam(r, "chainID"), chi.URLParam(r, "channelID"))
	if err != nil {
		failDomain(w, err)
		return
	}
	if out == nil {
		out = []wire.DeliveryView{}
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) listHeaders(w http.ResponseWriter, r *http.Request) {
	includeRevoked := r.URL.Query().Get("all") == "1"
	out, err := s.ex.ListHeaders(r.Context(), chi.URLParam(r, "chainID"), includeRevoked)
	if err != nil {
		failDomain(w, err)
		return
	}
	if out == nil {
		out = []wire.HeaderView{}
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) listAlerts(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit, _ := strconv.Atoi(q.Get("limit"))
	out, err := s.ex.ListAlerts(r.Context(), q.Get("chain_id"), q.Get("severity"), limit)
	if err != nil {
		failDomain(w, err)
		return
	}
	if out == nil {
		out = []wire.AlertView{}
	}
	writeJSON(w, http.StatusOK, out)
}
