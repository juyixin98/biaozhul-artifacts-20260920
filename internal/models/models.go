package models

import (
	"database/sql"
	"time"
)

type User struct {
	ID        int64     `db:"id" json:"id"`
	Username  string    `db:"username" json:"username"`
	KeyHash   string    `db:"key_hash" json:"-"`
	CreatedAt time.Time `db:"created_at" json:"created_at"`
}

type Object struct {
	ID        int64  `db:"id" json:"id"`
	Digest    string `db:"digest" json:"digest"`
	SizeBytes int64  `db:"size_bytes" json:"size_bytes"`
	Refcount  int32  `db:"refcount" json:"refcount"`
	Status    string `db:"status" json:"status"`
}

const (
	StatusUploading = "uploading"
	StatusReady     = "ready"
	StatusFailed    = "failed"
)

type Dataset struct {
	ID            int64          `db:"id" json:"id"`
	OwnerID       int64          `db:"owner_id" json:"owner_id"`
	Name          string         `db:"name" json:"name"`
	TotalSize     int64          `db:"total_size" json:"total_size"`
	ChunkSize     int32          `db:"chunk_size" json:"chunk_size"`
	ChunkCount    int32          `db:"chunk_count" json:"chunk_count"`
	WholeDigest   sql.NullString `db:"whole_digest" json:"whole_digest,omitempty"`
	WholeObjectID sql.NullInt64  `db:"whole_object_id" json:"-"`
	Status        string         `db:"status" json:"status"`
	CreatedAt     time.Time      `db:"created_at" json:"created_at"`
	PublishedAt   sql.NullTime   `db:"published_at" json:"published_at,omitempty"`
}

type Chunk struct {
	ID          int64     `db:"id" json:"id"`
	DatasetID   int64     `db:"dataset_id" json:"dataset_id"`
	ChunkIndex  int32     `db:"chunk_index" json:"chunk_index"`
	OffsetBytes int64     `db:"offset_bytes" json:"offset_bytes"`
	Length      int32     `db:"length" json:"length"`
	Digest      string    `db:"digest" json:"digest"`
	ObjectID    int64     `db:"object_id" json:"object_id"`
	UploadedAt  time.Time `db:"uploaded_at" json:"uploaded_at"`
}

type ModelVersion struct {
	ID               int64       `db:"id" json:"id"`
	OwnerID          int64       `db:"owner_id" json:"owner_id"`
	ModelName        string      `db:"model_name" json:"model_name"`
	Version          int32       `db:"version" json:"version"`
	DatasetID        int64       `db:"dataset_id" json:"dataset_id"`
	DatasetDigest    string      `db:"dataset_digest" json:"dataset_digest"`
	InputDim         int32       `db:"input_dim" json:"input_dim"`
	NumClasses       int32       `db:"num_classes" json:"num_classes"`
	Classes          StringArray `db:"classes" json:"classes"`
	ClassTableDigest string      `db:"class_table_digest" json:"class_table_digest"`
	Weights          []byte      `db:"weights" json:"-"`
	WeightDigest     string      `db:"weight_digest" json:"weight_digest"`
	CreatedAt        time.Time   `db:"created_at" json:"created_at"`
}

type ExperimentRecord struct {
	ID             int64     `db:"id" json:"id"`
	ModelVersionID int64     `db:"model_version_id" json:"model_version_id"`
	CallerID       int64     `db:"caller_id" json:"caller_id"`
	InputDigest    string    `db:"input_digest" json:"input_digest"`
	InputDim       int32     `db:"input_dim" json:"input_dim"`
	PredictedClass string    `db:"predicted_class" json:"predicted_class"`
	PredictedIndex int32     `db:"predicted_index" json:"predicted_index"`
	Confidence     float64   `db:"confidence" json:"confidence"`
	LatencyMs      float64   `db:"latency_ms" json:"latency_ms"`
	CreatedAt      time.Time `db:"created_at" json:"created_at"`
}
