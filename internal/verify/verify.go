// Package verify checks the synchronized result against the trusted sample and
// produces a final chain summary. Trust is anchored in the offline sample, not
// in anything a remote node reported.
package verify

import (
	"context"
	"encoding/hex"
	"fmt"

	"nodesync/internal/chain"
	"nodesync/internal/sample"
	"nodesync/internal/store"
)

// Summary is the final chain summary.
type Summary struct {
	PublishedHeights int    `json:"published_heights"`
	FirstHeight      int64  `json:"first_height"`
	LastHeight       int64  `json:"last_height"`
	GenesisHash      string `json:"genesis_hash"`
	TipHash          string `json:"tip_hash"`
	Digest           string `json:"digest"`
	MatchesSample    bool   `json:"matches_sample"`
	Explanation      string `json:"explanation,omitempty"`
}

// ChainSummary computes a summary of the published prefix and compares it to
// the trusted sample. ok is true only when the published chain is exactly the
// full trusted chain: contiguous from genesis to target tip, every link
// verified, and the digest equal to the sample digest.
func ChainSummary(ctx context.Context, st *store.Store, s *sample.Sample) (Summary, []chain.Block, bool, error) {
	published, err := st.Chain(ctx)
	if err != nil {
		return Summary{}, nil, false, err
	}
	if len(published) == 0 {
		return Summary{Explanation: "no blocks published"}, nil, false, nil
	}
	genesis, err := s.GenesisHashBytes()
	if err != nil {
		return Summary{}, nil, false, err
	}
	sum := Summary{
		PublishedHeights: len(published),
		FirstHeight:      published[0].Height,
		LastHeight:       published[len(published)-1].Height,
		GenesisHash:      hex.EncodeToString(published[0].Hash),
		TipHash:          hex.EncodeToString(published[len(published)-1].Hash),
		Digest:           hex.EncodeToString(sample.Digest(published)),
	}

	ok := true
	reasons := []string{}

	if err := chain.VerifyChain(genesis, published); err != nil {
		ok = false
		reasons = append(reasons, "published prefix failed verification: "+err.Error())
	}
	if published[0].Height != chain.GenesisHeight {
		ok = false
		reasons = append(reasons, fmt.Sprintf("first published height %d is not genesis", published[0].Height))
	}
	if published[len(published)-1].Height != s.TipHeight {
		ok = false
		reasons = append(reasons, fmt.Sprintf("published tip %d != trusted target %d",
			published[len(published)-1].Height, s.TipHeight))
	}
	if len(published) != s.Length+1 {
		ok = false
		reasons = append(reasons, fmt.Sprintf("published %d blocks, trusted chain has %d",
			len(published), s.Length+1))
	}
	if sum.Digest != s.Digest {
		ok = false
		reasons = append(reasons, "chain digest does not match trusted sample")
	}
	sum.MatchesSample = ok
	if !ok {
		// Keep summary readable.
		if len(reasons) > 3 {
			reasons = reasons[:3]
		}
		sum.Explanation = joinReasons(reasons)
	}
	return sum, published, ok, nil
}

func joinReasons(rs []string) string {
	out := ""
	for i, r := range rs {
		if i > 0 {
			out += "; "
		}
		out += r
	}
	return out
}
