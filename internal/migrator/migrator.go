// Package migrator performs a storage-version migration of Timer objects:
// it reads every object through the current served (conversion) API and
// writes it back, forcing the API server to re-encode it in the new storage
// version. The design deliberately goes through the apiserver (not etcd
// directly), so conversion webhook failures surface as per-object errors.
//
// Output is a JSON record file. On any per-object failure the tool still
// completes the sweep, records each failure, and exits non-zero; the record
// is the input to the recovery runbook (docs/ROLLBACK.md).
package migrator

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	apiextclientset "k8s.io/apiextensions-apiserver/pkg/client/clientset/clientset"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
)

// GVR of the Timer resource.
var GVR = schema.GroupVersionResource{
	Group: "timer.example.com", Version: "v1", Resource: "timers",
}

// ObjectRecord is one object's migration outcome.
type ObjectRecord struct {
	Namespace          string     `json:"namespace"`
	Name               string     `json:"name"`
	UID                string     `json:"uid,omitempty"`
	ResourceVersion    string     `json:"resourceVersion,omitempty"`
	StorageVersionHint string     `json:"storageVersionHint"`
	Status             string     `json:"status"` // migrated | skipped | failed
	Error              string     `json:"error,omitempty"`
	Attempts           int        `json:"attempts"`
	DurationMS         int64      `json:"durationMs"`
	MigratedAt         *time.Time `json:"migratedAt,omitempty"`
}

// Record is the whole run; written atomically at the end.
type Record struct {
	StartedAt     time.Time      `json:"startedAt"`
	FinishedAt    time.Time      `json:"finishedAt"`
	Direction     string         `json:"direction"` // alpha1->v1 | v1->alpha1
	Resource      string         `json:"resource"`
	StorageBefore []string       `json:"storageVersionsBefore"`
	StorageAfter  []string       `json:"storageVersionsAfter"`
	Total         int            `json:"total"`
	Migrated      int            `json:"migrated"`
	Skipped       int            `json:"skipped"`
	Failed        int            `json:"failed"`
	Objects       []ObjectRecord `json:"objects"`
}

// Options configures Run.
type Options struct {
	// ExtClient reads the CRD status.storedVersions (may be nil; then the
	// before/after fields are marked unavailable).
	ExtClient  apiextclientset.Interface
	Dynamic    dynamic.Interface
	Direction  string // "up" (default) or "down"
	RecordPath string // where to write the JSON record
	// MaxConflicts is the per-object conflict retry budget (default 3).
	MaxConflicts int
	Now          func() time.Time
}

// Run executes the sweep and writes the record file.
// It returns the record and a non-nil error when one or more objects failed.
func Run(ctx context.Context, opts Options) (*Record, error) {
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.MaxConflicts == 0 {
		opts.MaxConflicts = 3
	}
	dir := "alpha1->v1"
	gvr := GVR
	if opts.Direction == "down" {
		dir = "v1->alpha1"
		gvr = schema.GroupVersionResource{
			Group: "timer.example.com", Version: "v1alpha1", Resource: "timers",
		}
	}

	started := opts.Now()
	rec := &Record{
		StartedAt: started,
		Direction: dir,
		Resource:  "timers.timer.example.com",
		Objects:   []ObjectRecord{},
	}

	// Observe the CRD storage versions before/after (best effort).
	rec.StorageBefore = readStorageVersions(ctx, opts.ExtClient)

	list, err := opts.Dynamic.Resource(gvr).Namespace("").List(ctx, metav1.ListOptions{})
	if err != nil {
		// Even a list failure (e.g. the conversion webhook rejects the
		// stored objects for the requested version) must leave an
		// auditable record behind.
		rec.Failed = 1
		rec.Objects = append(rec.Objects, ObjectRecord{
			Name:   "<list>",
			Status: "failed",
			Error:  fmt.Sprintf("list via %s: %v", gvr.Version, err),
		})
		rec.StorageAfter = readStorageVersions(ctx, opts.ExtClient)
		rec.FinishedAt = opts.Now()
		if opts.RecordPath != "" {
			_ = writeRecordAtomic(opts.RecordPath, rec)
		}
		return rec, fmt.Errorf("listing timers via %s: %w", gvr.Version, err)
	}
	rec.Total = len(list.Items)

	// Deterministic ordering for reproducible records.
	items := list.Items
	sort.Slice(items, func(i, j int) bool {
		if items[i].GetNamespace() != items[j].GetNamespace() {
			return items[i].GetNamespace() < items[j].GetNamespace()
		}
		return items[i].GetName() < items[j].GetName()
	})

	for _, obj := range items {
		rec.Objects = append(rec.Objects, migrateOne(ctx, opts, obj, gvr))
	}

	for _, o := range rec.Objects {
		switch o.Status {
		case "migrated":
			rec.Migrated++
		case "skipped":
			rec.Skipped++
		case "failed":
			rec.Failed++
		}
	}

	rec.StorageAfter = readStorageVersions(ctx, opts.ExtClient)
	rec.FinishedAt = opts.Now()

	if opts.RecordPath != "" {
		if err := writeRecordAtomic(opts.RecordPath, rec); err != nil {
			return rec, fmt.Errorf("writing record file: %w", err)
		}
	}
	if rec.Failed > 0 {
		return rec, fmt.Errorf("%d of %d objects failed migration; see %s",
			rec.Failed, rec.Total, opts.RecordPath)
	}
	return rec, nil
}

// migrateOne reads the object (triggering any conversion), then updates it
// unchanged so the apiserver re-encodes it under the current storage
// version. Conflicts are retried; conversion failures are permanent for the
// run and recorded.
func migrateOne(ctx context.Context, opts Options, seed unstructured.Unstructured, gvr schema.GroupVersionResource) ObjectRecord {
	rec := ObjectRecord{
		Namespace: seed.GetNamespace(),
		Name:      seed.GetName(),
		UID:       string(seed.GetUID()),
	}
	start := time.Now()
	defer func() { rec.DurationMS = time.Since(start).Milliseconds() }()

	ns := seed.GetNamespace()
	name := seed.GetName()

	var lastErr error
	for attempt := 1; attempt <= opts.MaxConflicts; attempt++ {
		rec.Attempts = attempt

		live, err := opts.Dynamic.Resource(gvr).Namespace(ns).Get(ctx, name, metav1.GetOptions{})
		if err != nil {
			lastErr = fmt.Errorf("get: %w", err)
			if apierrors.IsNotFound(err) {
				rec.Status = "skipped"
				rec.Error = "object disappeared during sweep"
				return rec
			}
			break
		}
		rec.ResourceVersion = live.GetResourceVersion()

		// Update with the same body but a fresh resourceVersion: the
		// apiserver converts to storage version and rewrites etcd bytes.
		_, err = opts.Dynamic.Resource(gvr).Namespace(ns).Update(ctx, live, metav1.UpdateOptions{})
		if err == nil {
			t := opts.Now()
			rec.Status = "migrated"
			rec.MigratedAt = &t
			rec.StorageVersionHint = gvr.Version
			return rec
		}
		lastErr = err
		if !apierrors.IsConflict(err) {
			break
		}
		select {
		case <-ctx.Done():
			lastErr = ctx.Err()
		case <-time.After(time.Duration(attempt) * 100 * time.Millisecond):
		}
	}

	rec.Status = "failed"
	if lastErr != nil {
		rec.Error = lastErr.Error()
	}
	return rec
}

func readStorageVersions(ctx context.Context, c apiextclientset.Interface) []string {
	if c == nil {
		return nil
	}
	crd, err := c.ApiextensionsV1().CustomResourceDefinitions().Get(
		ctx, "timers.timer.example.com", metav1.GetOptions{})
	if err != nil {
		return []string{"<unavailable: " + trimErr(err) + ">"}
	}
	return crd.Status.StoredVersions
}

func writeRecordAtomic(path string, rec *Record) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	b, err := json.MarshalIndent(rec, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func trimErr(err error) string {
	if err == nil {
		return ""
	}
	s := err.Error()
	if len(s) > 80 {
		return s[:80]
	}
	return s
}
