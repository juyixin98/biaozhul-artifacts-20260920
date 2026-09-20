package app

import (
	"encoding/json"
	"time"
)

// Dataset is one uploaded dataset. OwnerID never serializes to clients.
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

// DatasetChunk records one received chunk's digest and size.
type DatasetChunk struct {
	DatasetID  int64     `db:"dataset_id" json:"dataset_id"`
	ChunkIndex int       `db:"chunk_index" json:"chunk_index"`
	SHA256     string    `db:"sha256" json:"sha256"`
	Size       int64     `db:"size" json:"size"`
	CreatedAt  time.Time `db:"created_at" json:"created_at"`
}

// DatasetView is a dataset plus per-chunk progress for resumable uploads.
type DatasetView struct {
	Dataset
	UploadedChunks []int `json:"uploaded_chunks"`
	MissingChunks  []int `json:"missing_chunks"`
}

// Model is a named container of immutable versions owned by one user.
type Model struct {
	ID        int64     `db:"id" json:"id"`
	OwnerID   string    `db:"owner_id" json:"-"`
	Name      string    `db:"name" json:"name"`
	CreatedAt time.Time `db:"created_at" json:"created_at"`
}

// ModelVersion is immutable after creation: dataset binding, input
// dimension, label table and weights can never be updated.
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

// ModelView is a model with all of its versions.
type ModelView struct {
	Model
	Versions []ModelVersion `json:"versions"`
}

// Experiment is a recorded run of a model version against a dataset.
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
