// 链摘要、来源证据与可信样例比对报告。
package syncer

import (
	"encoding/hex"
	"encoding/json"
	"fmt"

	"nodesync/internal/chain"
	"nodesync/internal/storage"
)

// ChainReport 是最终对外输出的完整报告。
type ChainReport struct {
	VerifiedTip        uint64                  `json:"verified_tip"`
	VerifiedHash       string                  `json:"verified_tip_hash"`
	TrustedSampleTip   uint64                  `json:"trusted_sample_tip"`
	SampleTipHash      string                  `json:"trusted_sample_tip_hash"`
	TipMatchesSample   bool                    `json:"tip_matches_trusted_sample"`
	ContinuousFromZero bool                    `json:"continuous_from_zero"`
	Checkpoints        []CheckpointResult      `json:"checkpoints"`
	Provenance         map[string]int          `json:"blocks_served_by_node"`
	Summary            *Summary                `json:"run_summary"`
	Evidence           []storage.EvidenceEvent `json:"evidence"`
	Peers              []storage.PeerStatus    `json:"peers_observed"`
}

// CheckpointResult 是单个可信检查点的比对结果。
type CheckpointResult struct {
	Height     uint64 `json:"height"`
	WantHash   string `json:"sample_hash"`
	GotHash    string `json:"verified_hash"`
	Matches    bool   `json:"matches"`
	SourceNode string `json:"source_node"`
}

// BuildReport 从持久化状态构建最终报告并执行样例一致性比对。
func BuildReport(store *storage.Store, sample chain.TrustedSample, summary *Summary) (*ChainReport, error) {
	blocks, err := store.BlocksMap()
	if err != nil {
		return nil, err
	}
	r := &ChainReport{
		TrustedSampleTip: sample.TipHeight,
		SampleTipHash:    sample.Checkpoints[sample.TipHeight],
		Summary:          summary,
		Provenance:       map[string]int{},
	}

	// 连续性：0..tip 无缺口
	var maxH uint64
	for h := range blocks {
		if h > maxH {
			maxH = h
		}
	}
	if len(blocks) > 0 {
		cont := true
		for h := uint64(0); h <= maxH; h++ {
			if _, ok := blocks[h]; !ok {
				cont = false
				break
			}
		}
		r.ContinuousFromZero = cont
		r.VerifiedTip = maxH
		r.VerifiedHash = hex.EncodeToString(blocks[maxH].Hash())
		r.TipMatchesSample = r.VerifiedHash == r.SampleTipHash
	}

	heights := make([]uint64, 0, len(sample.Checkpoints))
	for h := range sample.Checkpoints {
		heights = append(heights, h)
	}
	sortUint64(heights)
	for _, h := range heights {
		cp := CheckpointResult{Height: h, WantHash: sample.Checkpoints[h]}
		if b, ok := blocks[h]; ok {
			cp.GotHash = hex.EncodeToString(b.Hash())
			cp.Matches = cp.GotHash == cp.WantHash
			if node, _ := store.BlockProvenance(h); node != "" {
				cp.SourceNode = node
			}
		}
		r.Checkpoints = append(r.Checkpoints, cp)
	}

	// 来源证据：每个高度来自哪个节点
	for h := range blocks {
		node, err := store.BlockProvenance(h)
		if err != nil {
			return nil, err
		}
		r.Provenance[node]++
	}

	if r.Evidence, err = store.Evidence(0); err != nil {
		return nil, err
	}
	if r.Peers, err = store.AllPeerStatus(); err != nil {
		return nil, err
	}
	return r, nil
}

// PrintText 输出人可读的文本摘要。
func (r *ChainReport) PrintText() string {
	out := fmt.Sprintf(`===== 节点区间同步器 · 最终链摘要 =====
已验证检查点高度 : %d
已验证链尖哈希   : %s
可信样例链尖高度 : %d
样例链尖哈希     : %s
链尖与样例一致   : %v
自高度0连续无缺口: %v
运行目标高度     : %d  到达目标: %v  被取消: %v
节点宣称最大高度 : %d （仅参考，非已验证进度）
成功拉取段数     : %d  已提交段数: %d  被拒段数: %d
`,
		r.VerifiedTip, r.VerifiedHash,
		r.TrustedSampleTip, r.SampleTipHash,
		r.TipMatchesSample, r.ContinuousFromZero,
		r.Summary.TargetTip, r.Summary.ReachedTarget, r.Summary.Cancelled,
		r.Summary.AdvertisedMax,
		r.Summary.SegmentsFetched, r.Summary.SegmentsCommitted, r.Summary.SegmentsRejected)

	out += "\n-- 可信检查点比对 --\n"
	for _, cp := range r.Checkpoints {
		out += fmt.Sprintf("  高度 %-3d 匹配=%-5v 来源=%s\n    样例: %s\n    实测: %s\n",
			cp.Height, cp.Matches, cp.SourceNode, cp.WantHash, cp.GotHash)
	}
	out += "\n-- 区块来源分布（来源证据）--\n"
	for node, n := range r.Provenance {
		out += fmt.Sprintf("  %s: %d 个区块\n", node, n)
	}
	if len(r.Summary.Warnings) > 0 {
		out += "\n-- 警告 --\n"
		for _, w := range r.Summary.Warnings {
			out += "  ! " + w + "\n"
		}
	}
	out += "\n-- 观测到的节点宣称状态 --\n"
	for _, p := range r.Peers {
		out += fmt.Sprintf("  %s: 宣称高度=%d hash=%s 错误=%q\n",
			p.NodeID, p.AdvertisedHeight, p.AdvertisedHash, p.LastError)
	}
	return out
}

// JSON 输出机器可读报告。
func (r *ChainReport) JSON() ([]byte, error) { return json.MarshalIndent(r, "", "  ") }

func sortUint64(s []uint64) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j-1] > s[j]; j-- {
			s[j-1], s[j] = s[j], s[j-1]
		}
	}
}
