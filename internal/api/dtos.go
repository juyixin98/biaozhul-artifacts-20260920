package api

import (
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/vfxqueue/renderq/internal/db/dbgen"
)

// toTime converts a pgtype timestamptz to a *UTC time (nil when NULL).
func toTime(t pgtype.Timestamptz) *time.Time {
	if !t.Valid {
		return nil
	}
	u := t.Time.UTC()
	return &u
}

type projectDTO struct {
	ID        uuid.UUID  `json:"id"`
	Name      string     `json:"name"`
	OwnerID   uuid.UUID  `json:"ownerId"`
	CreatedAt *time.Time `json:"createdAt"`
}

func projectDTOFrom(p dbgen.Project) projectDTO {
	return projectDTO{ID: p.ID, Name: p.Name, OwnerID: p.OwnerID, CreatedAt: toTime(p.CreatedAt)}
}

type userDTO struct {
	ID       uuid.UUID `json:"id"`
	Username string    `json:"username"`
	Role     string    `json:"role"`
}

func userDTOFrom(u dbgen.User) userDTO {
	return userDTO{ID: u.ID, Username: u.Username, Role: u.Role}
}

type assetDTO struct {
	ID        uuid.UUID  `json:"id"`
	ProjectID uuid.UUID  `json:"projectId"`
	RelPath   string     `json:"relPath"`
	Width     int32      `json:"width"`
	Height    int32      `json:"height"`
	SizeBytes int64      `json:"sizeBytes"`
	SHA256    string     `json:"sha256"`
	CreatedAt *time.Time `json:"createdAt"`
}

func assetDTOFrom(a dbgen.Asset) assetDTO {
	return assetDTO{
		ID: a.ID, ProjectID: a.ProjectID, RelPath: a.RelPath,
		Width: a.Width, Height: a.Height, SizeBytes: a.SizeBytes,
		SHA256: a.Sha256, CreatedAt: toTime(a.CreatedAt),
	}
}

type compositionDTO struct {
	ID        uuid.UUID  `json:"id"`
	ProjectID uuid.UUID  `json:"projectId"`
	Name      string     `json:"name"`
	CreatedAt *time.Time `json:"createdAt"`
}

func compositionDTOFrom(c dbgen.Composition) compositionDTO {
	return compositionDTO{ID: c.ID, ProjectID: c.ProjectID, Name: c.Name, CreatedAt: toTime(c.CreatedAt)}
}

type versionResourceDTO struct {
	LayerID string    `json:"layerId"`
	AssetID uuid.UUID `json:"assetId"`
	SHA256  string    `json:"sha256"`
}

type versionDTO struct {
	ID             uuid.UUID            `json:"id"`
	CompositionID  uuid.UUID            `json:"compositionId"`
	VersionNo      int64                `json:"versionNo"`
	Manifest       map[string]any       `json:"manifest"`
	ManifestSHA256 string               `json:"manifestSha256"`
	Resources      []versionResourceDTO `json:"resources,omitempty"`
	CreatedAt      *time.Time           `json:"createdAt"`
}

type jobDTO struct {
	ID          uuid.UUID        `json:"id"`
	VersionID   uuid.UUID        `json:"versionId"`
	ProjectID   uuid.UUID        `json:"projectId"`
	Priority    int32            `json:"priority"`
	FrameStart  int32            `json:"frameStart"`
	FrameEnd    int32            `json:"frameEnd"`
	Status      string           `json:"status"`
	Error       string           `json:"error,omitempty"`
	EnqueuedAt  *time.Time       `json:"enqueuedAt"`
	StartedAt   *time.Time       `json:"startedAt,omitempty"`
	FinishedAt  *time.Time       `json:"finishedAt,omitempty"`
	CanceledBy  *uuid.UUID       `json:"canceledBy,omitempty"`
	CreatedAt   *time.Time       `json:"createdAt"`
	FrameCounts map[string]int64 `json:"frameCounts,omitempty"`
}

func jobDTOFrom(j dbgen.Job) jobDTO {
	return jobDTO{
		ID: j.ID, VersionID: j.VersionID, ProjectID: j.ProjectID,
		Priority: j.Priority, FrameStart: j.FrameStart, FrameEnd: j.FrameEnd,
		Status: j.Status, Error: j.Error,
		EnqueuedAt: toTime(j.EnqueuedAt), StartedAt: toTime(j.StartedAt),
		FinishedAt: toTime(j.FinishedAt), CanceledBy: j.CanceledBy,
		CreatedAt: toTime(j.CreatedAt),
	}
}

type frameDTO struct {
	ID           uuid.UUID  `json:"id"`
	FrameNo      int32      `json:"frameNo"`
	Status       string     `json:"status"`
	Attempts     int32      `json:"attempts"`
	MaxAttempts  int32      `json:"maxAttempts"`
	Generation   int64      `json:"generation"`
	OutputSHA256 string     `json:"outputSha256,omitempty"`
	OutputSize   int64      `json:"outputSize,omitempty"`
	LastError    string     `json:"lastError,omitempty"`
	LeasedBy     *string    `json:"leasedBy,omitempty"`
	UpdatedAt    *time.Time `json:"updatedAt"`
}

func frameDTOFrom(f dbgen.Frame) frameDTO {
	return frameDTO{
		ID: f.ID, FrameNo: f.FrameNo, Status: f.Status,
		Attempts: f.Attempts, MaxAttempts: f.MaxAttempts, Generation: f.Generation,
		OutputSHA256: f.OutputSha256, OutputSize: f.OutputSize, LastError: f.LastError,
		LeasedBy: f.LeasedBy, UpdatedAt: toTime(f.UpdatedAt),
	}
}
