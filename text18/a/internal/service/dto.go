package service

import (
	"time"

	"sircc/internal/store"
)

// UserView is the user shape exposed by the API.
type UserView struct {
	ID       string `json:"id"`
	Username string `json:"username"`
	FullName string `json:"full_name"`
	Role     string `json:"role"`
}

func userView(u store.User) UserView {
	return UserView{ID: u.ID.String(), Username: u.Username, FullName: u.FullName, Role: u.Role}
}

type PhaseView struct {
	ID            string    `json:"id"`
	Phase         string    `json:"phase"`
	ActorID       string    `json:"actor_id"`
	ActorUsername string    `json:"actor_username,omitempty"`
	RequestID     *string   `json:"request_id,omitempty"`
	EnteredAt     time.Time `json:"entered_at"`
}

type MemberView struct {
	UserID     string    `json:"user_id"`
	Username   string    `json:"username"`
	FullName   string    `json:"full_name"`
	CaseRole   string    `json:"case_role"`
	AssignedBy *string   `json:"assigned_by,omitempty"`
	CreatedAt  time.Time `json:"created_at"`
}

type IncidentView struct {
	ID             string     `json:"id"`
	Title          string     `json:"title"`
	Description    string     `json:"description"`
	Severity       string     `json:"severity"`
	Status         string     `json:"status"`
	Version        int64      `json:"version"`
	RootCause      *string    `json:"root_cause,omitempty"`
	LessonsLearned *string    `json:"lessons_learned,omitempty"`
	DetectedAt     time.Time  `json:"detected_at"`
	ClosedAt       *time.Time `json:"closed_at,omitempty"`
	CreatedBy      string     `json:"created_by"`
	CreatedAt      time.Time  `json:"created_at"`
	UpdatedAt      time.Time  `json:"updated_at"`

	Phases  []PhaseView   `json:"phases,omitempty"`
	Members []MemberView  `json:"members,omitempty"`
	Metrics *PhaseMetrics `json:"metrics,omitempty"`
}

type ActionItemView struct {
	ID            string    `json:"id"`
	IncidentID    string    `json:"incident_id"`
	Description   string    `json:"description"`
	OwnerUserID   string    `json:"owner_user_id"`
	OwnerUsername string    `json:"owner_username,omitempty"`
	OwnerFullName string    `json:"owner_full_name,omitempty"`
	DueAt         time.Time `json:"due_at"`
	DueVersion    int32     `json:"due_version"`
	Status        string    `json:"status"`
	CreatedBy     string    `json:"created_by"`
	CreatedAt     time.Time `json:"created_at"`
	UpdatedAt     time.Time `json:"updated_at"`
}

type ReminderView struct {
	ID           string    `json:"id"`
	ActionItemID string    `json:"action_item_id"`
	IncidentID   string    `json:"incident_id"`
	DueVersion   int32     `json:"due_version"`
	DueAt        time.Time `json:"due_at"`
	Message      string    `json:"message"`
	CreatedAt    time.Time `json:"created_at"`
}

type AuditView struct {
	ID         string         `json:"id"`
	Action     string         `json:"action"`
	ActorID    string         `json:"actor_id"`
	FromStatus *string        `json:"from_status,omitempty"`
	ToStatus   *string        `json:"to_status,omitempty"`
	RequestID  *string        `json:"request_id,omitempty"`
	Detail     map[string]any `json:"detail"`
	CreatedAt  time.Time      `json:"created_at"`
}
