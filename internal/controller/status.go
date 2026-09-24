package controller

import (
	"strconv"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	snapshotv1alpha1 "snapshotcontroller/api/v1alpha1"
)

const (
	condReady = "Ready"
)

// setPhase replaces the phase/condition fields in target to describe phase,
// always binding the status to generation. When status moves forward to a new
// generation, Ready-only outputs of the previous generation are cleared here
// so stale digest data can never be read as the current generation's result.
func setPhase(status *snapshotv1alpha1.SnapshotStatus, s *snapshotv1alpha1.Snapshot, phase snapshotv1alpha1.SnapshotPhase) {
	if status.ObservedGeneration != s.Generation {
		status.Digest = ""
		status.Algorithm = ""
		status.Manifest = ""
		status.FileCount = 0
		status.TotalBytes = 0
	}
	status.Phase = phase
	status.ObservedGeneration = s.Generation

	now := metav1.Now()
	cond := metav1.Condition{
		Type:               condReady,
		LastTransitionTime: now,
	}
	switch phase {
	case snapshotv1alpha1.PhaseReady:
		cond.Status = metav1.ConditionTrue
		cond.Reason = string(phase)
		cond.Message = "snapshot digest recorded"
	case snapshotv1alpha1.PhaseFailed:
		cond.Status = metav1.ConditionFalse
		cond.Reason = status.FailureReason
		if cond.Reason == "" {
			cond.Reason = string(phase)
		}
		cond.Message = status.FailureMessage
	case snapshotv1alpha1.PhaseRunning:
		cond.Status = metav1.ConditionFalse
		cond.Reason = string(phase)
		cond.Message = "worker job is running"
	default: // Pending
		cond.Status = metav1.ConditionFalse
		cond.Reason = string(phase)
		cond.Message = "waiting to start worker job"
	}
	upsertCondition(&status.Conditions, cond)
}

// setDigestStatus copies the worker-produced digest fields for the current
// generation, then flips phase to Ready.
func setDigestStatus(status *snapshotv1alpha1.SnapshotStatus, s *snapshotv1alpha1.Snapshot, digest, algorithm, manifest string, fileCount int, totalBytes int64, jobName string) {
	status.Digest = digest
	status.Algorithm = algorithm
	status.Manifest = manifest
	status.FileCount = fileCount
	status.TotalBytes = totalBytes
	status.FailureReason = ""
	status.FailureMessage = ""
	status.JobRef = &snapshotv1alpha1.JobRef{Name: jobName, Generation: s.Generation}
	setPhase(status, s, snapshotv1alpha1.PhaseReady)
}

// setFailure records a deterministic failure for the current generation.
func setFailure(status *snapshotv1alpha1.SnapshotStatus, s *snapshotv1alpha1.Snapshot, reason, message string, jobName string) {
	status.FailureReason = reason
	status.FailureMessage = message
	status.JobRef = &snapshotv1alpha1.JobRef{Name: jobName, Generation: s.Generation}
	setPhase(status, s, snapshotv1alpha1.PhaseFailed)
}

func upsertCondition(conditions *[]metav1.Condition, c metav1.Condition) {
	for i := range *conditions {
		if (*conditions)[i].Type == c.Type {
			if (*conditions)[i].Status == c.Status {
				c.LastTransitionTime = (*conditions)[i].LastTransitionTime
			}
			(*conditions)[i] = c
			return
		}
	}
	*conditions = append(*conditions, c)
}

// resultData is the parsed worker result ConfigMap content.
type resultData struct {
	digest     string
	algorithm  string
	manifest   string
	fileCount  int
	totalBytes int64
	generation int64
}

// parseResultConfigMap extracts/validates the fields the worker writes.
func parseResultConfigMap(data map[string]string) (resultData, error) {
	get := func(k string) (string, error) {
		v, ok := data[k]
		if !ok || v == "" {
			return "", errField(k)
		}
		return v, nil
	}
	digest, err := get("digest")
	if err != nil {
		return resultData{}, err
	}
	algorithm, err := get("algorithm")
	if err != nil {
		return resultData{}, err
	}
	manifest := data["manifest"] // may technically be empty for empty sources

	fileCount := 0
	if v := data["fileCount"]; v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return resultData{}, errField("fileCount")
		}
		fileCount = n
	}
	var totalBytes int64
	if v := data["totalBytes"]; v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			return resultData{}, errField("totalBytes")
		}
		totalBytes = n
	}
	var gen int64
	if v := data["generation"]; v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			return resultData{}, errField("generation")
		}
		gen = n
	}
	return resultData{
		digest:     digest,
		algorithm:  algorithm,
		manifest:   manifest,
		fileCount:  fileCount,
		totalBytes: totalBytes,
		generation: gen,
	}, nil
}

type fieldError struct{ field string }

func (e fieldError) Error() string { return "missing or invalid result field: " + e.field }
func errField(f string) error      { return fieldError{field: f} }
