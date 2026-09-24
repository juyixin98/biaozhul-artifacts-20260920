// Package resolver parses local OCI image indexes, verifies every blob in a
// chosen artifact's dependency chain, and selects exactly one image manifest
// for a requested platform.
//
// Everything that can be verified against real bytes is verified: digest and
// declared size for every index, nested index, manifest, image config and
// layer. No registry traffic is generated; only the local blob store is read.
package resolver

import (
	"encoding/json"
	"errors"
	"fmt"

	"github.example.com/ocimultipick/internal/blobstore"
	"github.example.com/ocimultipick/internal/digest"
	"github.example.com/ocimultipick/internal/oci"
	"github.example.com/ocimultipick/internal/selector"
)

// ChainEntry is one node in the resolved dependency chain, ordered root first.
type ChainEntry struct {
	Role      string `json:"role"`
	Digest    string `json:"digest"`
	Size      int64  `json:"size"`
	MediaType string `json:"mediaType,omitempty"`
}

// Result is the output of a successful resolution.
type Result struct {
	Repository    string
	RootDigest    digest.Digest
	Platform      selector.Platform
	Manifest      oci.Descriptor
	Config        oci.Descriptor
	Layers        []oci.Descriptor
	ConfigOS      string
	ConfigArch    string
	ConfigVariant string
	Chain         []ChainEntry
}

// ContentStore is the storage surface the resolver needs. blobstore.Store
// implements it; tests can provide fakes.
//
// ReadVerified returns identityDigest: the digest the returned bytes actually
// hash to, after checking that it equals desc.Digest and that the byte count
// equals desc.Size. Resolver traversal keys its cycle detection on
// identityDigest rather than the claimed digest, so a descriptor that aliases
// one blob's bytes under another digest cannot be used to hide a reference
// cycle.
type ContentStore interface {
	ReadVerified(repo string, desc oci.Descriptor) (identityDigest string, body []byte, err error)
	StatSize(repo string, d digest.Digest) (int64, error)
}

// Resolver performs resolution against a content store.
type Resolver struct {
	store ContentStore
}

// New constructs a Resolver.
func New(store ContentStore) *Resolver { return &Resolver{store: store} }

// Error codes surfaced to the API layer.
const (
	CodeInvalidReference = "invalid_reference"
	CodeBlobMissing      = "blob_missing"
	CodeDigestMismatch   = "digest_mismatch"
	CodeSizeMismatch     = "size_mismatch"
	CodeMalformedJSON    = "malformed_json"
	CodeCycleDetected    = "cycle_detected"
	CodeUnknownManifest  = "unknown_manifest_type"
	CodeNoMatch          = "no_platform_match"
	CodeAmbiguous        = "platform_ambiguous"
	CodeConfigPlatform   = "config_platform_mismatch"
)

// Error carries a stable machine-readable code alongside the message.
type Error struct {
	Code   string
	Detail string
}

func (e *Error) Error() string { return e.Code + ": " + e.Detail }

func codeError(code, format string, args ...any) *Error {
	return &Error{Code: code, Detail: fmt.Sprintf(format, args...)}
}

type leaf struct {
	descriptor oci.Descriptor // descriptor from the parent index, platform attached
	chain      []ChainEntry   // chain from the root index down to this manifest
}

// Resolve resolves rootDigest within repo for platform p.
func (r *Resolver) Resolve(repo string, rootDigest digest.Digest, p selector.Platform) (*Result, error) {
	// A blob visited twice *on the current DFS path* is a reference cycle.
	// The set keys on the digest the bytes hash to (see ContentStore), not on
	// the digest string claimed by the descriptor.
	onPath := map[string]bool{}

	var leaves []leaf
	var visit func(desc oci.Descriptor, chain []ChainEntry) error
	visit = func(desc oci.Descriptor, chain []ChainEntry) error {
		if _, err := digest.Parse(desc.Digest); err != nil {
			return codeError(CodeInvalidReference, "%s", err.Error())
		}

		// Read the actual bytes, verifying digest and declared size in one
		// pass. identity is what the bytes really are.
		identity, body, err := r.store.ReadVerified(repo, desc)
		if err != nil {
			return wrapVerifyError(desc, err)
		}
		if onPath[identity] {
			return codeError(CodeCycleDetected,
				"reference cycle at %s reached via chain %s", identity, chainDigestList(chain))
		}
		kind, err := classify(body)
		if err != nil {
			return codeError(errCodeOf(err), "blob %s: %s", identity, err.Error())
		}

		entry := ChainEntry{
			Role:      map[bool]string{true: "index", false: "manifest"}[kind == "index"],
			Digest:    identity,
			Size:      desc.Size,
			MediaType: desc.MediaType,
		}
		nextChain := append(append([]ChainEntry{}, chain...), entry)
		onPath[identity] = true
		defer delete(onPath, identity)

		if kind == "index" {
			var idx oci.Index
			if err := json.Unmarshal(body, &idx); err != nil {
				return codeError(CodeMalformedJSON, "index %s: %s", identity, err.Error())
			}
			if len(idx.Manifests) == 0 {
				return codeError(CodeUnknownManifest, "index %s has no manifests", identity)
			}
			for _, child := range idx.Manifests {
				if err := visit(child, nextChain); err != nil {
					return err
				}
			}
			return nil
		}
		// Record the manifest with its verified identity digest (which equals
		// the claimed digest for an unaliased store).
		identityDesc := desc
		identityDesc.Digest = identity
		leaves = append(leaves, leaf{descriptor: identityDesc, chain: nextChain})
		return nil
	}

	rootSize, err := r.store.StatSize(repo, rootDigest)
	if err != nil {
		return nil, wrapVerifyError(oci.Descriptor{Digest: rootDigest.String()}, err)
	}
	root := oci.Descriptor{
		MediaType: oci.MediaTypeImageIndex,
		Digest:    rootDigest.String(),
		Size:      rootSize,
	}
	if err := visit(root, nil); err != nil {
		return nil, err
	}

	// Select a leaf by the platform descriptors carried in the index.
	candidates := make([]oci.Descriptor, 0, len(leaves))
	for _, lf := range leaves {
		candidates = append(candidates, lf.descriptor)
	}
	chosen, err := selector.Select(p, candidates)
	if err != nil {
		var amb *selector.AmbiguousError
		var noMatch *selector.NoMatchError
		switch {
		case errors.As(err, &amb):
			return nil, codeError(CodeAmbiguous, "%s", amb.Error())
		case errors.As(err, &noMatch):
			return nil, codeError(CodeNoMatch, "%s", noMatch.Error())
		default:
			return nil, codeError(CodeNoMatch, "%s", err.Error())
		}
	}

	var chosenLeaf *leaf
	for i := range leaves {
		if leaves[i].descriptor.Digest == chosen.Digest {
			chosenLeaf = &leaves[i]
			break
		}
	}

	// Verify the chosen manifest's config and every one of its layers against
	// the real bytes. The manifest itself was already verified during traversal.
	var manifest oci.Manifest
	if _, body, err := r.store.ReadVerified(repo, chosen); err != nil {
		return nil, wrapVerifyError(chosen, err)
	} else if err := json.Unmarshal(body, &manifest); err != nil {
		return nil, codeError(CodeMalformedJSON, "manifest %s: %s", chosen.Digest, err.Error())
	}
	if manifest.Config.Digest == "" {
		return nil, codeError(CodeInvalidReference, "manifest %s has no config descriptor", chosen.Digest)
	}
	var cfg oci.ImageConfig
	if _, cfgBody, err := r.store.ReadVerified(repo, manifest.Config); err != nil {
		return nil, wrapVerifyError(manifest.Config, err)
	} else if err := json.Unmarshal(cfgBody, &cfg); err != nil {
		return nil, codeError(CodeMalformedJSON, "config %s: %s", manifest.Config.Digest, err.Error())
	}
	for _, layer := range manifest.Layers {
		if _, _, err := r.store.ReadVerified(repo, layer); err != nil {
			return nil, wrapVerifyError(layer, err)
		}
	}

	// The image config blob is authoritative: cross-check it against the
	// platform advertised in the index and against the requested platform.
	adv := chosen.Platform
	if adv == nil {
		return nil, codeError(CodeConfigPlatform, "manifest %s has no platform descriptor in index", chosen.Digest)
	}
	if cfg.OS != adv.OS || cfg.Architecture != adv.Architecture || cfg.Variant != adv.Variant {
		return nil, codeError(CodeConfigPlatform,
			"config %s reports platform %s/%s%s but index advertises %s/%s%s",
			manifest.Config.Digest,
			cfg.OS, cfg.Architecture, variantSuffix(cfg.Variant),
			adv.OS, adv.Architecture, variantSuffix(adv.Variant))
	}
	if cfg.OS != p.OS || cfg.Architecture != p.Arch {
		return nil, codeError(CodeConfigPlatform,
			"config %s platform %s/%s does not satisfy requested %s",
			manifest.Config.Digest, cfg.OS, cfg.Architecture, p)
	}
	if p.Variant != "" && cfg.Variant != p.Variant {
		return nil, codeError(CodeConfigPlatform,
			"config %s variant %q does not satisfy requested variant %q",
			manifest.Config.Digest, cfg.Variant, p.Variant)
	}

	// Assemble the full dependency chain: index(es) -> manifest -> config -> layers.
	chain := append([]ChainEntry{}, chosenLeaf.chain...)
	chain = append(chain, ChainEntry{
		Role: "config", Digest: manifest.Config.Digest,
		Size: manifest.Config.Size, MediaType: manifest.Config.MediaType,
	})
	for _, l := range manifest.Layers {
		chain = append(chain, ChainEntry{Role: "layer", Digest: l.Digest, Size: l.Size, MediaType: l.MediaType})
	}

	return &Result{
		Repository:    repo,
		RootDigest:    rootDigest,
		Platform:      p,
		Manifest:      chosen,
		Config:        manifest.Config,
		Layers:        manifest.Layers,
		ConfigOS:      cfg.OS,
		ConfigArch:    cfg.Architecture,
		ConfigVariant: cfg.Variant,
		Chain:         chain,
	}, nil
}

var errMalformed = errors.New("malformed")
var errUnknownType = errors.New("unknown type")

func errCodeOf(err error) string {
	switch {
	case errors.Is(err, errMalformed):
		return CodeMalformedJSON
	default:
		return CodeUnknownManifest
	}
}

// classify determines whether a verified JSON blob is an image index or an
// image manifest by inspecting the JSON structure itself rather than trusting
// declared mediaType strings.
func classify(body []byte) (string, error) {
	var probe struct {
		Manifests *[]json.RawMessage `json:"manifests"`
		Config    *json.RawMessage   `json:"config"`
	}
	if err := json.Unmarshal(body, &probe); err != nil {
		return "", fmt.Errorf("%w: %s", errMalformed, err.Error())
	}
	switch {
	case probe.Manifests != nil:
		return "index", nil
	case probe.Config != nil:
		return "manifest", nil
	default:
		return "", fmt.Errorf("%w: blob has neither \"manifests\" (index) nor \"config\" (manifest) fields", errUnknownType)
	}
}

func wrapVerifyError(desc oci.Descriptor, err error) error {
	switch {
	case errors.Is(err, blobstore.ErrNotFound):
		return codeError(CodeBlobMissing, "blob %s is not present locally", desc.Digest)
	case errors.Is(err, digest.ErrSizeMismatch):
		return codeError(CodeSizeMismatch, "%s", err.Error())
	case errors.Is(err, digest.ErrMismatch):
		return codeError(CodeDigestMismatch, "%s", err.Error())
	case errors.Is(err, digest.ErrInvalid):
		return codeError(CodeInvalidReference, "%s", err.Error())
	default:
		return err
	}
}

func chainDigestList(chain []ChainEntry) string {
	out := ""
	for i, c := range chain {
		if i > 0 {
			out += " -> "
		}
		out += c.Digest
	}
	return out
}

func variantSuffix(v string) string {
	if v == "" {
		return ""
	}
	return "/" + v
}
