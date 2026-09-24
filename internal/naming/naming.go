// Package naming computes the content digest and the deterministic child
// object name. Everything here is pure and therefore trivially testable.
package naming

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"

	corev1 "k8s.io/api/core/v1"
)

// ContentDigest returns the lowercase hex sha256 of the canonical JSON
// encoding of (format, data). The encoding is produced by encoding/json over
// a struct with fixed field order, so identical content always yields the same
// digest regardless of map iteration order at call sites.
func ContentDigest(format, data string) string {
	canonical := struct {
		Format string `json:"format"`
		Data   string `json:"data"`
	}{Format: format, Data: data}
	b, err := json.Marshal(canonical)
	if err != nil {
		// json.Marshal of this struct cannot fail; panic would indicate a
		// programming error rather than bad input.
		panic(fmt.Errorf("naming: canonical marshal failed: %w", err))
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// ChildName returns the deterministic child object name for one
// ConfigSnapshot. It embeds the first 16 hex chars of the content digest, so a
// brand-new content version appears as a new object name while repeated events
// and full resyncs resolve to the exact same name and never create twice.
//
// Length budget: 253 (max ConfigMap name) - len("cfg-") - len("-") - 16 = 230
// chars for the owner name, and owner names are DNS-1123 subdomains already.
func ChildName(ownerName, digest string) string {
	const suffixLen = 16
	suffix := digest
	if len(suffix) > suffixLen {
		suffix = suffix[:suffixLen]
	}
	return fmt.Sprintf("cfg-%s-%s", ownerName, suffix)
}

// DataKey is the key under which the payload is stored in the child ConfigMap.
const DataKey = "config"

// FormatKey is the annotation recording the payload format.
const FormatKey = "config.example.com/format"

// DigestKey is the annotation recording the full content digest.
const DigestKey = "config.example.com/digest"

// OwnerNameKey is the annotation recording the owning ConfigSnapshot name.
const OwnerNameKey = "config.example.com/owner"

// ChildLabels returns the labels every child object carries. They let
// garbage-collection list candidates cheaply, while ownership/UID checks stay
// authoritative.
func ChildLabels(ownerName string) map[string]string {
	return map[string]string{
		"app.kubernetes.io/managed-by": "config-distributor",
		"config.example.com/owner":     ownerName,
	}
}

// PayloadFor returns the (format, data) recorded on a child ConfigMap.
func PayloadFor(cm *corev1.ConfigMap) (format, data string) {
	if cm.Annotations != nil {
		format = cm.Annotations[FormatKey]
	}
	if cm.Data != nil {
		data = cm.Data[DataKey]
	}
	return
}
