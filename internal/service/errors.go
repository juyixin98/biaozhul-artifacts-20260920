package service

import "errors"

// Business errors. Handlers map these to specific HTTP status codes.
var (
	ErrNotFound            = errors.New("resource not found")
	ErrForbidden           = errors.New("you are not a member of this job")
	ErrRoleDenied          = errors.New("your role may not perform this action on this job")
	ErrNotAssignedReviewer = errors.New("you are not an assigned reviewer on this job")
	ErrNotApprover         = errors.New("only the assigned project manager may sign off")
	ErrInvalidInput        = errors.New("invalid request")

	ErrReviewerRoster   = errors.New("a job requires between 1 and 8 distinct reviewers")
	ErrReviewerRole     = errors.New("all reviewers must have the reviewer role")
	ErrDesignerConflict = errors.New("designer cannot also be the PM or a reviewer")
	ErrPMConflict       = errors.New("the PM cannot also be a reviewer")

	ErrRoundInactive = errors.New("this review round is no longer active; open the latest version")
	ErrJobClosed     = errors.New("this job is already approved and closed")

	ErrFailReasonRequired  = errors.New("a failed item requires a non-empty reason")
	ErrUnknownItem         = errors.New("opinion references a checklist item not in this round")
	ErrOutcomeInvalid      = errors.New("outcome must be pass, fail or na")
	ErrPendingItems        = errors.New("approval blocked: pending checklist items remain")
	ErrFailedItems         = errors.New("approval blocked: failed checklist items must be resolved")
	ErrReviewersIncomplete = errors.New("approval blocked: not every assigned reviewer has completed all items")

	ErrConflict        = errors.New("the opinion was modified by another request; refetch and retry")
	ErrSelfApproval    = errors.New("the designer may not approve their own job")
	ErrFileTooLarge    = errors.New("uploaded file exceeds the size limit")
	ErrFileType        = errors.New("only local PDF and PNG files are accepted")
	ErrVersionMismatch = errors.New("the requested file version does not exist on this job")
)
