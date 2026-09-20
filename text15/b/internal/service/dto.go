package service

import (
	"time"

	"proofcycle/internal/model"
)

// ---- request payloads ----

type CreateUserInput struct {
	Name string `json:"name"`
	Role string `json:"role"`
}

type ChecklistInput struct {
	Code        string `json:"code"`
	Description string `json:"description"`
}

type CreateJobInput struct {
	Name        string           `json:"name"`
	PMID        int64            `json:"pm_id"`
	ReviewerIDs []int64          `json:"reviewer_ids"`
	Checklist   []ChecklistInput `json:"checklist"`
}

type OpinionItemInput struct {
	// ChecklistCode identifies which snapshot item this verdict is for.
	ChecklistCode string `json:"checklist_code"`
	Verdict       string `json:"verdict"`
	Reason        string `json:"reason"`
	// ExpectedVersion is the opinion row version the client last saw
	// (0 when the opinion does not exist yet). A mismatch rejects the write.
	ExpectedVersion int `json:"expected_version"`
}

type SubmitOpinionsInput struct {
	// Version identifies the file version / review round explicitly, so an
	// opinion can never be silently filed against a newer revision.
	Version int                `json:"version"`
	Items   []OpinionItemInput `json:"items"`
}

// ---- response DTOs ----

type UserDTO struct {
	ID        int64     `json:"id"`
	Name      string    `json:"name"`
	Role      string    `json:"role"`
	APIToken  string    `json:"api_token,omitempty"`
	CreatedAt time.Time `json:"created_at"`
}

func toUserDTO(u *model.User, includeToken bool) UserDTO {
	d := UserDTO{ID: u.ID, Name: u.Name, Role: u.Role, CreatedAt: u.CreatedAt}
	if includeToken {
		d.APIToken = u.APIToken
	}
	return d
}

type JobDTO struct {
	ID             int64     `json:"id"`
	Name           string    `json:"name"`
	Status         string    `json:"status"`
	CurrentVersion int       `json:"current_version"`
	DesignerID     int64     `json:"designer_id"`
	DesignerName   string    `json:"designer_name"`
	PMID           int64     `json:"pm_id"`
	PMName         string    `json:"pm_name"`
	ReviewerIDs    []int64   `json:"reviewer_ids"`
	CreatedAt      time.Time `json:"created_at"`
	UpdatedAt      time.Time `json:"updated_at"`
}

type VersionDTO struct {
	Version    int       `json:"version"`
	FileName   string    `json:"file_name"`
	SHA256     string    `json:"sha256"`
	SizeBytes  int64     `json:"size_bytes"`
	MimeType   string    `json:"mime_type"`
	Status     string    `json:"status"`
	UploadedBy int64     `json:"uploaded_by"`
	CreatedAt  time.Time `json:"created_at"`
}

type OpinionView struct {
	ReviewerID   int64     `json:"reviewer_id"`
	ReviewerName string    `json:"reviewer_name"`
	Verdict      string    `json:"verdict"`
	Reason       string    `json:"reason,omitempty"`
	RowVersion   int       `json:"row_version"`
	UpdatedAt    time.Time `json:"updated_at"`
}

type ChecklistItemView struct {
	Code        string        `json:"code"`
	Description string        `json:"description"`
	Opinions    []OpinionView `json:"opinions"`
}

type RoundView struct {
	Version       int                 `json:"version"`
	Status        string              `json:"status"`
	File          VersionDTO          `json:"file"`
	Checklist     []ChecklistItemView `json:"checklist"`
	PendingItems  []string            `json:"pending_items,omitempty"`
	FailedItems   []string            `json:"failed_items,omitempty"`
	SignoffBy     *int64              `json:"signoff_by,omitempty"`
	SignoffByName string              `json:"signoff_by_name,omitempty"`
	SignoffAt     *time.Time          `json:"signoff_at,omitempty"`
	SignoffBasis  string              `json:"signoff_basis,omitempty"`
	CreatedAt     time.Time           `json:"created_at"`
}

type JobDetailDTO struct {
	JobDTO
	Rounds []RoundView `json:"rounds"`
}
