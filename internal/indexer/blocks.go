package indexer

import (
	"context"
	"encoding/json"
	"errors"

	"forkindexer/internal/model"

	"github.com/jackc/pgx/v5"
)

func jsonMarshal(v any) ([]byte, error) { return json.Marshal(v) }

// ErrNotCanonical is returned when a caller asks for a chain rooted at a hash
// that is not part of the current canonical chain.
var ErrNotCanonical = errors.New("hash is not on the canonical chain")

// storedBlock is a block row as the indexer works with it internally.
type storedBlock struct {
	Hash         string
	ParentHash   string
	Height       int64
	Status       string
	ReceivedSeq  int64
	Transactions []model.Transfer
}

// BlockInfo is the externally visible block.
type BlockInfo struct {
	Hash         string           `json:"hash"`
	ParentHash   string           `json:"parentHash"`
	Height       int64            `json:"height"`
	Status       string           `json:"status"`
	Canonical    bool             `json:"canonical"`
	ReceivedSeq  int64            `json:"receivedSeq"`
	Transactions []model.Transfer `json:"transactions"`
}

// HeadInfo describes the canonical chain tip.
type HeadInfo struct {
	Hash   string `json:"hash"`
	Height int64  `json:"height"`
}

// TransactionRow is one indexed transfer on the canonical chain.
type TransactionRow struct {
	BlockHash string `json:"blockHash"`
	Height    int64  `json:"height"`
	TxIndex   int    `json:"txIndex"`
	From      string `json:"from"`
	To        string `json:"to"`
	Amount    string `json:"amount"`
}

// StateInfo is the durable indexer state.
type StateInfo struct {
	HeadHash     string `json:"headHash"`
	HeadHeight   int64  `json:"headHeight"`
	IngestSeq    int64  `json:"ingestSeq"`
	StreamOffset int64  `json:"streamOffset"`
}

func scanBlock(row pgx.Row) (*storedBlock, error) {
	var b storedBlock
	var rawTxs []byte
	if err := row.Scan(&b.Hash, &b.ParentHash, &b.Height, &b.Status, &b.ReceivedSeq, &rawTxs); err != nil {
		return nil, err
	}
	if err := json.Unmarshal(rawTxs, &b.Transactions); err != nil {
		return nil, err
	}
	return &b, nil
}

const blockCols = `hash, parent_hash, height, status, received_seq, transactions`

func loadBlock(ctx context.Context, tx pgx.Tx, hash string) (*storedBlock, error) {
	return scanBlock(tx.QueryRow(ctx,
		`SELECT `+blockCols+` FROM blocks WHERE hash = $1`, hash))
}
