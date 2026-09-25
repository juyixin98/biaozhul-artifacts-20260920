package service

import (
	"crypto/hmac"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"

	"buildprovenance/internal/provenance"
)

// VerifyReport is the result of independently verifying one artifact's
// transitive provenance.
type VerifyReport struct {
	ArtifactID  string  `json:"artifactId"`
	Complete    bool    `json:"complete"`    // every provenance check passed
	Issues      []Issue `json:"issues"`      // empty when Complete
	Depth       int     `json:"depth"`       // max upstream depth walked
	Records     int     `json:"records"`     // distinct records checked
	BlobsHashed int     `json:"blobsHashed"` // content blobs re-read and hashed
}

// Verify performs a fully independent verification pass for artifactID.
//
// Nothing stored in indexes or records is trusted: every blob is re-read and
// re-hashed, every tool definition is re-resolved and re-digested from files
// on disk, every record hash and HMAC is recomputed, and the whole attestation
// log chain is checked.
func (s *Service) Verify(artifactID string) (*VerifyReport, error) {
	rep := &VerifyReport{ArtifactID: artifactID}
	visited := map[string]bool{}

	var walk func(id string)
	walk = func(id string) {
		if visited[id] {
			return
		}
		visited[id] = true
		rep.Records++

		art, err := s.artifacts.Get(id)
		if err != nil {
			if errors.Is(err, provenance.ErrNotFound) {
				rep.Issues = append(rep.Issues, Issue{Code: CodeArtifactNotFound, ArtifactID: id,
					Message: fmt.Sprintf("artifact %s is not registered", id)})
				return
			}
			rep.Issues = append(rep.Issues, Issue{Code: CodeArtifactNotFound, ArtifactID: id, Message: err.Error()})
			return
		}

		// 1. Artifact blob exists and re-hashes to the indexed digest.
		blob, err := s.artifacts.CAS().Get(art.Digest)
		if err != nil {
			rep.Issues = append(rep.Issues, Issue{Code: CodeArtifactBlobMissing, ArtifactID: id,
				Message: fmt.Sprintf("artifact content blob %s is missing from cache: %v", art.Digest, err)})
		} else {
			rep.BlobsHashed++
			if got := provenance.DigestBytes(blob); got != art.Digest {
				rep.Issues = append(rep.Issues, Issue{Code: CodeArtifactDigestMismatch, ArtifactID: id,
					Message: fmt.Sprintf("artifact blob hashes to %s, index claims %s", got, art.Digest)})
			}
			if int64(len(blob)) != art.Size {
				rep.Issues = append(rep.Issues, Issue{Code: CodeOutputSizeMismatch, ArtifactID: id,
					Message: fmt.Sprintf("artifact size %d, indexed as %d", len(blob), art.Size)})
			}
		}

		// 2. Attestation record exists.
		rec, err := s.log.Get(id)
		if err != nil {
			rep.Issues = append(rep.Issues, Issue{Code: CodeRecordNotFound, ArtifactID: id,
				Message: fmt.Sprintf("no attestation record for artifact %s", id)})
			return
		}

		// 3. Record content hash recomputed independently.
		gotHash, err := provenance.RecordHash(rec)
		if err != nil {
			rep.Issues = append(rep.Issues, Issue{Code: CodeRecordHashMismatch, ArtifactID: id, Message: err.Error()})
		} else if gotHash != rec.RecordHash {
			rep.Issues = append(rep.Issues, Issue{Code: CodeRecordHashMismatch, ArtifactID: id,
				Message: fmt.Sprintf("record content hashes to %s, stored recordHash is %s (attestation tampered)", gotHash, rec.RecordHash)})
		}

		// 4. HMAC signature over the recomputed hash.
		wantSig := s.log.Sign(gotHash)
		if !hmac.Equal(decodeHex(rec.Sig), decodeHex(wantSig)) {
			rep.Issues = append(rep.Issues, Issue{Code: CodeRecordSigInvalid, ArtifactID: id,
				Message: "record HMAC signature does not verify (forged or key-changed record)"})
		}

		// 5. Artifact index agrees with the record's claimed output.
		if art.RecordHash != "" && art.RecordHash != rec.RecordHash {
			rep.Issues = append(rep.Issues, Issue{Code: CodeUpstreamRecordMismatch, ArtifactID: id,
				Message: fmt.Sprintf("artifact index points at record %s but record hashes to %s", art.RecordHash, rec.RecordHash)})
		}
		if rec.OutputDigest != art.Digest {
			rep.Issues = append(rep.Issues, Issue{Code: CodeRecordOutputMismatch, ArtifactID: id,
				Message: fmt.Sprintf("record binds output digest %s but artifact index claims %s", rec.OutputDigest, art.Digest)})
		}
		// And the record's output digest matches actual bytes.
		if len(blob) > 0 && rec.OutputDigest != provenance.DigestBytes(blob) {
			rep.Issues = append(rep.Issues, Issue{Code: CodeRecordOutputMismatch, ArtifactID: id,
				Message: "record output digest does not match re-hashed artifact bytes"})
		}

		// 6. Tool still exists on disk and re-digests identically.
		t, err := s.GetTool(rec.ToolName)
		if err != nil {
			rep.Issues = append(rep.Issues, Issue{Code: CodeToolNotFound, ArtifactID: id,
				Message: fmt.Sprintf("attested tool %q is not registered on this host: %v", rec.ToolName, err)})
		} else {
			d, _, err := s.rehashTool(t)
			if err != nil {
				rep.Issues = append(rep.Issues, Issue{Code: CodeToolDigestMismatch, ArtifactID: id,
					Message: fmt.Sprintf("tool %q cannot be re-resolved: %v", rec.ToolName, err)})
			} else if d != rec.ToolDigest {
				rep.Issues = append(rep.Issues, Issue{Code: CodeToolDigestMismatch, ArtifactID: id,
					Message: fmt.Sprintf("tool %q now digests to %s, attestation claims %s (interpreter/script/command/env changed)",
						rec.ToolName, d.Short(), rec.ToolDigest.Short())})
			}
		}

		// 7. Every recorded input re-resolves and re-hashes identically.
		upstreamIDs := make([]string, 0, len(rec.Upstreams))
		for _, in := range rec.Inputs {
			switch in.Kind {
			case "source":
				src, err := s.sources.Get(in.Path)
				if err != nil {
					rep.Issues = append(rep.Issues, Issue{Code: CodeSourceNotFound, ArtifactID: id,
						Message: fmt.Sprintf("input source %q no longer registered: %v", in.Path, err)})
					continue
				}
				b, err := s.sourcesCAS().Get(src.Digest)
				if err != nil {
					rep.Issues = append(rep.Issues, Issue{Code: CodeInputBlobMissing, ArtifactID: id,
						Message: fmt.Sprintf("source %q blob %s missing: %v", in.Path, src.Digest, err)})
					continue
				}
				rep.BlobsHashed++
				if got := provenance.DigestBytes(b); got != in.Digest || src.Digest != in.Digest {
					rep.Issues = append(rep.Issues, Issue{Code: CodeInputDigestMismatch, ArtifactID: id,
						Message: fmt.Sprintf("source %q: recorded digest %s does not match re-hashed bytes %s", in.Path, in.Digest, got)})
				}
			case "artifact":
				up, err := s.artifacts.Get(in.ArtifactID)
				if err != nil {
					rep.Issues = append(rep.Issues, Issue{Code: CodeArtifactNotFound, ArtifactID: id,
						Message: fmt.Sprintf("upstream artifact %s missing from index", in.ArtifactID)})
					upstreamIDs = append(upstreamIDs, in.ArtifactID)
					continue
				}
				b, err := s.artifacts.CAS().Get(up.Digest)
				if err != nil {
					rep.Issues = append(rep.Issues, Issue{Code: CodeInputBlobMissing, ArtifactID: id,
						Message: fmt.Sprintf("upstream artifact %s blob missing: %v", in.ArtifactID, err)})
				} else {
					rep.BlobsHashed++
					if got := provenance.DigestBytes(b); got != in.Digest {
						rep.Issues = append(rep.Issues, Issue{Code: CodeInputDigestMismatch, ArtifactID: id,
							Message: fmt.Sprintf("upstream %s: recorded input digest %s, actual %s", in.ArtifactID, in.Digest, got)})
					}
				}
				// The upstream map must carry this artifact's *current*
				// record hash; swapping it is the classic chain forgery.
				upRec, err := s.log.Get(in.ArtifactID)
				if err != nil {
					rep.Issues = append(rep.Issues, Issue{Code: CodeRecordNotFound, ArtifactID: id,
						Message: fmt.Sprintf("upstream %s attestation missing", in.ArtifactID)})
				} else {
					claimed, present := rec.Upstreams[in.ArtifactID]
					if !present {
						rep.Issues = append(rep.Issues, Issue{Code: CodeUpstreamHashMismatch, ArtifactID: id,
							Message: fmt.Sprintf("input %s is absent from the upstream record-hash map", in.ArtifactID)})
					} else if claimed != upRec.RecordHash {
						rep.Issues = append(rep.Issues, Issue{Code: CodeUpstreamHashMismatch, ArtifactID: id,
							Message: fmt.Sprintf("upstream %s record-hash mismatch: record claims %s, upstream attests %s",
								in.ArtifactID, claimed[:12], upRec.RecordHash[:12])})
					}
				}
				upstreamIDs = append(upstreamIDs, in.ArtifactID)
			default:
				rep.Issues = append(rep.Issues, Issue{Code: CodeInputDigestMismatch, ArtifactID: id,
					Message: fmt.Sprintf("input slot %s has unknown kind %q", in.Slot, in.Kind)})
			}
		}

		// 8. Recurse.
		sort.Strings(upstreamIDs)
		for _, u := range upstreamIDs {
			walk(u)
		}
	}
	walk(artifactID)

	// 9. Global structural checks: cycles anywhere reachable from the target,
	// and the append-only log chain.
	if cyc, err := s.findCycle(artifactID); err != nil {
		rep.Issues = append(rep.Issues, Issue{Code: CodeCycleDetected, ArtifactID: artifactID, Message: err.Error()})
	} else if cyc != nil {
		rep.Issues = append(rep.Issues, Issue{Code: CodeCycleDetected, ArtifactID: artifactID,
			Message: fmt.Sprintf("forged cycle in upstream graph: %v", cyc)})
	}
	if err := s.log.VerifyChain(); err != nil {
		rep.Issues = append(rep.Issues, Issue{Code: CodeLogChainBroken, ArtifactID: artifactID,
			Message: fmt.Sprintf("append-only attestation log chain failed: %v", err)})
	}

	rep.Depth = s.depth(artifactID, map[string]int{}, map[string]bool{})
	rep.Complete = len(rep.Issues) == 0
	return rep, nil
}

func (s *Service) depth(id string, memo map[string]int, visiting map[string]bool) int {
	if d, ok := memo[id]; ok {
		return d
	}
	if visiting[id] { // forged cycle; depth is undefined, stay finite
		return 0
	}
	rec, err := s.log.Get(id)
	if err != nil {
		return 0
	}
	visiting[id] = true
	best := 0
	for u := range rec.Upstreams {
		if d := s.depth(u, memo, visiting) + 1; d > best {
			best = d
		}
	}
	visiting[id] = false
	memo[id] = best
	return best
}

func decodeHex(x string) []byte {
	b, err := hex.DecodeString(x)
	if err != nil {
		return nil
	}
	return b
}
