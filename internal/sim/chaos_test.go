package sim

import (
	"fmt"
	mathrand "math/rand"
	"testing"
)

// TestRandomizedSafety 是验收核心：在大量随机故障序列下，引擎内置的
// 安全不变量必须在每个 tick 都成立——
//
//  1. election_safety：任意一个任期至多出现一个领导者；
//  2. commit_agreement：任意时刻任意两个存活节点的已提交日志逐索引一致；
//  3. ack_safety：运行结束时每条被确认的写入仍存在于多数派节点日志中。
//
// 每个序列最后愈合网络并断言 log_match（最终日志收敛）。
func TestRandomizedSafety(t *testing.T) {
	const seeds = 24
	const ticks = 220
	const healAt = 160
	for seed := int64(1); seed <= seeds; seed++ {
		t.Run(fmt.Sprintf("seed=%d", seed), func(t *testing.T) {
			rng := mathrand.New(mathrand.NewSource(seed * 1009))
			s := &Script{
				Seed:       seed * 7919,
				Ticks:      ticks,
				Nodes:      3,
				Heartbeat:  3,
				ElectionLo: 6,
				ElectionHi: 12,
				Network:    NetConfig{DelayTicks: 1, Jitter: 2, LossRate: 0.05, DupRate: 0.03},
			}
			crashed := map[int]bool{}
			isolated := map[int]bool{}
			writeSeq := 0
			nextWrite := 8
			for at := 1; at < healAt; at++ {
				// 约 8% 的 tick 对随机节点制造/恢复崩溃。
				if rng.Intn(100) < 8 {
					id := rng.Intn(3)
					if !crashed[id] {
						s.Events = append(s.Events, Event{At: at, Kind: "crash", Target: &id})
						crashed[id] = true
					}
				}
				if rng.Intn(100) < 10 {
					id := rng.Intn(3)
					if crashed[id] {
						s.Events = append(s.Events, Event{At: at, Kind: "restart", Target: &id})
						crashed[id] = false
					}
				}
				// 约 7% 的 tick 切换某节点隔离状态。
				if rng.Intn(100) < 7 {
					id := rng.Intn(3)
					s.Events = append(s.Events, Event{At: at, Kind: "isolate", Isolated: &id, Heal: isolated[id]})
					isolated[id] = !isolated[id]
				}
				// 每隔若干 tick 尝试一次客户端写（发给当前领导者，由引擎解析）。
				if at >= nextWrite {
					writeSeq++
					s.Events = append(s.Events, Event{
						At: at, Kind: "client_write",
						Client: fmt.Sprintf("w%d", writeSeq),
						Data:   fmt.Sprintf("data-%d", writeSeq),
					})
					nextWrite = at + 3 + rng.Intn(5)
				}
			}
			// 愈合阶段：重启全部崩溃节点、解除全部隔离、重置链路。
			for id := 0; id < 3; id++ {
				if crashed[id] {
					i := id
					s.Events = append(s.Events, Event{At: healAt, Kind: "restart", Target: &i})
				}
				if isolated[id] {
					i := id
					s.Events = append(s.Events, Event{At: healAt, Kind: "isolate", Isolated: &i, Heal: true})
				}
			}
			s.Events = append(s.Events, Event{At: healAt, Kind: "network_reset"})
			// 愈合后给足够长的安静期完成追赶，然后要求最终一致。
			s.Events = append(s.Events, Event{At: ticks - 5, Kind: "log_match"})

			if err := s.fillDefaultsAndValidate(); err != nil {
				t.Fatalf("生成的脚本非法: %v", err)
			}
			r := Run(s)
			if !r.OK {
				for _, f := range r.Failures {
					t.Errorf("seed=%d tick=%d 不变量 [%s] 被违反: %s", seed, f.At, f.Kind, f.Message)
				}
			}
			// 最终所有存活节点的 commitIndex 必须一致，且全部已确认写入都在其中。
			if r.OK {
				ci := r.Nodes[0].CommitIndex
				for _, n := range r.Nodes[1:] {
					if n.CommitIndex != ci {
						t.Errorf("seed=%d 愈合后 commitIndex 不一致: %d vs %d", seed, ci, n.CommitIndex)
					}
				}
			}
		})
	}
}
