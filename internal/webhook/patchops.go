package webhook

import (
	"encoding/json"

	jsonpatch "github.com/evanphx/json-patch/v5"
)

// jsonMergeTo6902 turns the diff between original and modified (produced by
// applying a defaulting merge patch) into an RFC 6902 JSON Patch document,
// which is the only patch type admission.k8s.io/v1 accepts.
//
// We compute the sparse merge patch first because that is the natural shape
// for defaulting ("set spec.priority if absent"), then expand it into
// add/replace operations. Nested objects whose parent does not exist in the
// original document get an explicit "add" of the whole subtree first, so the
// patch never targets a missing parent.
func jsonMergeTo6902(original, modified []byte) []byte {
	mergeDoc, err := jsonpatch.CreateMergePatch(original, modified)
	if err != nil {
		return []byte("[]")
	}
	var sparse map[string]any
	if err := json.Unmarshal(mergeDoc, &sparse); err != nil {
		return []byte("[]")
	}
	var orig map[string]any
	if err := json.Unmarshal(original, &orig); err != nil {
		orig = map[string]any{}
	}
	ops := expandMerge([]rfc6902Op{}, "", sparse, orig)
	out, err := json.Marshal(ops)
	if err != nil {
		return []byte("[]")
	}
	return out
}

type rfc6902Op struct {
	Op    string          `json:"op"`
	Path  string          `json:"path"`
	Value json.RawMessage `json:"value,omitempty"`
}

// expandMerge walks a sparse merge document relative to original and emits
// RFC 6902 ops. Arrays and scalars are replaced atomically; objects descend.
func expandMerge(ops []rfc6902Op, path string, patch, original map[string]any) []rfc6902Op {
	for key, val := range patch {
		childPath := path + "/" + escapeJSONPointer(key)
		nested, isObject := val.(map[string]any)
		existing, parentHasKey := original[key]

		if !isObject {
			// Scalar or array leaf.
			ops = append(ops, makeOp(addOrReplace(parentHasKey && existing != nil), childPath, val))
			continue
		}

		existingObj, parentIsObject := existing.(map[string]any)
		switch {
		case !parentHasKey || existing == nil:
			// Parent container missing: add the whole subtree in one op.
			ops = append(ops, makeOp("add", childPath, nested))
		case !parentIsObject:
			// Parent exists but is a scalar/array: replace it wholesale.
			ops = append(ops, makeOp("replace", childPath, nested))
		default:
			ops = expandMerge(ops, childPath, nested, existingObj)
		}
	}
	return ops
}

func makeOp(op, path string, value any) rfc6902Op {
	raw, err := json.Marshal(value)
	if err != nil {
		raw = []byte("null")
	}
	return rfc6902Op{Op: op, Path: path, Value: raw}
}

func addOrReplace(exists bool) string {
	if exists {
		return "replace"
	}
	return "add"
}

func escapeJSONPointer(s string) string {
	out := make([]byte, 0, len(s))
	for i := 0; i < len(s); i++ {
		switch s[i] {
		case '~':
			out = append(out, '~', '0')
		case '/':
			out = append(out, '~', '1')
		default:
			out = append(out, s[i])
		}
	}
	return string(out)
}
