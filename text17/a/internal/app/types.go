package app

import (
	"encoding/json"
	"time"
)

type Dataset struct {
	ID          int64      `db:"id" json:"id"`
	OwnerID     string     `db:"owner_id" json:"-"`
	Name        string     `db:"name" json:"name"`
	Status      string     `db:"status" json:"status"`
	TotalSize   int64      `db:"total_size" json:"total_size"`
	ChunkSize   int64      `db:"chunk_size" json:"chunk_size"`
	ChunkCount  int        `db:"chunk_count" json:"chunk_count"`
	SHA256      string     `db:"sha256" json:"sha256"`
	FileID      *int64     `db:"file_id" json:"file_id,omitempty"`
	CreatedAt   time.Time  `db:"created_at" json:"created_at"`
	PublishedAt *time.Time `db:"published_at" json:"published_at,omitempty"`
}

type DatasetChunk struct {
	DatasetID  int64     `db:"dataset_id" json:"dataset_id"`
	ChunkIndex int       `db:"chunk_index" json:"chunk_index"`
	SHA256     string    `db:"sha256" json:"sha256"`
	Size       int64     `db:"size" json:"size"`
	CreatedAt  time.Time `db:"created_at" json:"created_at"`
}

type Model struct {
	ID        int64     `db:"id" json:"id"`
	OwnerID   string    `db:"owner_id" json:"-"`
	Name      string    `db:"name" json:"name"`
	CreatedAt time.Time `db:"created_at" json:"created_at"`
}

// ModelVersion is immutable once created: there is deliberately no update
// path for dataset binding, weights or labels.
type ModelVersion struct {
	ID            int64           `db:"id" json:"id"`
	ModelID       int64           `db:"model_id" json:"model_id"`
	Version       int             `db:"version" json:"version"`
	DatasetID     int64           `db:"dataset_id" json:"dataset_id"`
	DatasetSHA256 string          `db:"dataset_sha256" json:"dataset_sha256"`
	InputDim      int             `db:"input_dim" json:"input_dim"`
	Labels        json.RawMessage `db:"labels" json:"labels"`
	Weights       json.RawMessage `db:"weights" json:"-"`
	CreatedAt     time.Time       `db:"created_at" json:"created_at"`
}

type Experiment struct {
	ID             int64           `db:"id" json:"id"`
	OwnerID        string          `db:"owner_id" json:"-"`
	Name           string          `db:"name" json:"name"`
	ModelVersionID int64           `db:"model_version_id" json:"model_version_id"`
	DatasetID      int64           `db:"dataset_id" json:"dataset_id"`
	Metrics        json.RawMessage `db:"metrics" json:"metrics"`
	Notes          string          `db:"notes" json:"notes"`
	CreatedAt      time.Time       `db:"created_at" json:"created_at"`
}
