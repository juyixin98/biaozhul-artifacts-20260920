package controller

import (
	"fmt"
	"strconv"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// FinalizerName blocks Snapshot deletion until owned Jobs and result
// ConfigMaps have actually been deleted.
const FinalizerName = "snapshot.example.com/cleanup"

const (
	// LabelGeneration records the .metadata.generation of the Snapshot that an
	// owned Job or result ConfigMap belongs to. A late result from a stale
	// generation is rejected.
	LabelGeneration = "snapshot.example.com/generation"
	// LabelComponent distinguishes worker Jobs from result ConfigMaps.
	LabelComponent = "snapshot.example.com/component"

	ComponentJob    = "snapshot-job"
	ComponentResult = "snapshot-result"

	// JobNamePrefix / ResultNamePrefix derive deterministic child names.
	JobNamePrefix    = "snap-"
	ResultNamePrefix = "snap-result-"

	// ResultKey is the key under which the worker stores result.json.
	ResultKey = "result.json"
)

// Failure reasons surfaced on status.
const (
	ReasonJobFailed     = "JobFailed"
	ReasonResultInvalid = "ResultInvalid"
	ReasonResultMissing = "ResultMissing"
)

// WorkerResult is the JSON document published by the snapshot Job in its
// result ConfigMap under key result.json. It proves that a real archive file
// was produced and hashed; the controller never invents these values.
type WorkerResult struct {
	APIVersion string       `json:"apiVersion"`
	Kind       string       `json:"kind"`
	Generation int64        `json:"generation"`
	Source     string       `json:"source"`
	OutputFile string       `json:"outputFile"`
	SHA256     string       `json:"sha256"`
	SizeBytes  int64        `json:"sizeBytes"`
	Files      []ResultFile `json:"files"`
	// ComputedAt is RFC3339 UTC, filled by the worker.
	ComputedAt string `json:"computedAt"`
}

// ResultFile mirrors v1alpha1.ResultFile in the wire protocol.
type ResultFile struct {
	Path   string `json:"path"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}

// JobName returns the deterministic Job name for a generation.
func JobName(snapshotName string, generation int64) string {
	return fmt.Sprintf("%s%s-g%d", JobNamePrefix, snapshotName, generation)
}

// ResultName returns the deterministic result ConfigMap name for a generation.
func ResultName(snapshotName string, generation int64) string {
	return fmt.Sprintf("%s%s-g%d", ResultNamePrefix, snapshotName, generation)
}

// generationLabel parses the generation label; returns 0,false when absent or
// unparsable.
func generationLabel(obj metav1.Object) (int64, bool) {
	l := obj.GetLabels()
	if l == nil {
		return 0, false
	}
	v, ok := l[LabelGeneration]
	if !ok {
		return 0, false
	}
	g, err := strconv.ParseInt(v, 10, 64)
	if err != nil {
		return 0, false
	}
	return g, true
}

func componentLabels(component string, generation int64) map[string]string {
	return map[string]string{
		LabelComponent:  component,
		LabelGeneration: strconv.FormatInt(generation, 10),
	}
}
