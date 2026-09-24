package controller

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"hash/fnv"
	"strconv"

	snapshotv1alpha1 "snapshotcontroller/api/v1alpha1"
)

const (
	// Finalizer blocks Snapshot deletion until owned Jobs/ConfigMaps are gone.
	Finalizer = "snapshot.example.com/finalizer"

	labelPartOf     = "app.kubernetes.io/part-of"
	labelManagedBy  = "snapshot.example.com/managed-by"
	labelSnapshot   = "snapshot.example.com/snapshot"
	labelGeneration = "snapshot.example.com/generation"

	partOfValue = "recoverable-snapshot-controller"
	managedBy   = "snapshot-controller"
)

// generationName returns the deterministic name of the worker Job (and result
// ConfigMap) for a snapshot/generation pair. Determinism is what makes repeat
// reconciles idempotent: the same generation always maps to the same Job.
//
// The 32-bit FNV suffix keeps names inside the 63-char label/name limit while
// remaining stable; a short sha256 prefix is added for extra uniqueness
// across snapshots that happen to share an FNV bucket.
func generationName(snapshotName string, generation int64) string {
	h := fnv.New32a()
	h.Write([]byte(fmt.Sprintf("%s/%d", snapshotName, generation)))
	suffix := strconv.FormatUint(uint64(h.Sum32()), 10)

	sh := sha256.Sum256([]byte(fmt.Sprintf("%s:%d", snapshotName, generation)))
	prefix := hex.EncodeToString(sh[:])[:4]

	name := "snap-" + snapshotName + "-" + strconv.FormatInt(generation, 10) + "-" + prefix + suffix
	if len(name) > 63 {
		// Keep the suffix, truncate the snapshot name.
		tail := "-" + strconv.FormatInt(generation, 10) + "-" + prefix + suffix
		keep := 63 - len("snap-") - len(tail)
		if keep < 1 {
			keep = 1
		}
		if len(snapshotName) > keep {
			snapshotName = snapshotName[:keep]
		}
		name = "snap-" + snapshotName + tail
	}
	return name
}

// JobName is the exported form of generationName for tests.
func JobName(snapshotName string, generation int64) string {
	return generationName(snapshotName, generation)
}

// selectorForSnapshot returns a label selector string matching every resource
// created for a snapshot (all generations).
func selectorForSnapshot(snapshotName string) string {
	return fmt.Sprintf("%s=%s,%s=%s",
		labelPartOf, partOfValue,
		labelSnapshot, snapshotName)
}

// standardLabels returns the labels every created resource carries.
func standardLabels(snapshotName string, generation int64) map[string]string {
	return map[string]string{
		labelPartOf:     partOfValue,
		labelManagedBy:  managedBy,
		labelSnapshot:   snapshotName,
		labelGeneration: strconv.FormatInt(generation, 10),
	}
}

// generationLabel is a small accessor kept for readability.
func generationLabel(s *snapshotv1alpha1.Snapshot) string {
	return strconv.FormatInt(s.Generation, 10)
}
