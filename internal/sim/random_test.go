package sim

import (
	"fmt"
	"math/rand"
	"path/filepath"
	"testing"
)

// TestRandomizedSafety 在大量随机场景（随机丢包/重复/乱序、随机分区、
// 崩溃-重启与客户端写入）下反复运行，断言 Raft 安全性不变量：
// 任意时刻被任一节点标记为已提交的索引，其（任期, 命令）在所有
// 存活节点上完全一致，且日志匹配性质始终成立。
func TestRandomizedSafety(t *testing.T) {
	iterations := 200
	if testing.Short() {
		iterations = 8
	}

	for seed := int64(1); seed <= int64(iterations); seed++ {
		sc := randomScenario(seed, t.TempDir())
		r, err := Run(sc)
		if err != nil {
			t.Fatalf("seed %d: run error: %v", seed, err)
		}
		if !r.InvariantCheck.Passed {
			t.Fatalf("seed %d: safety violation:\n%s\nscenario:\n%s",
				seed, dumpErrors(r.InvariantCheck.Errors), dumpScenario(sc))
		}
		// 所有被报告 committed 的写入必须出现在连续的已提交前缀中。
		committedClients := map[string]bool{}
		for _, e := range r.Committed {
			if e.Client != "" {
				committedClients[e.Client] = true
			}
		}
		for _, p := range r.Proposals {
			if p.Status == "committed" && !committedClients[p.Client] {
				t.Fatalf("seed %d: proposal %q marked committed but absent from committed prefix",
					seed, p.Client)
			}
		}
	}
}

func dumpErrors(errs []string) string {
	out := ""
	for _, e := range errs {
		out += "  - " + e + "\n"
	}
	return out
}

func dumpScenario(sc *Scenario) string {
	out := fmt.Sprintf("seed=%d end=%d loss=%.2f dup=%.2f jitter=%.2f\n",
		sc.Seed, sc.EndTime, sc.Network.LossPct, sc.Network.DupPct, sc.Network.JitterPct)
	for _, e := range sc.Events {
		out += fmt.Sprintf("  t=%d %s", e.Time, e.Type)
		if e.Node != nil {
			out += fmt.Sprintf(" node=%d", e.Node.ID)
		}
		if e.Client != "" {
			out += " client=" + e.Client
		}
		out += "\n"
	}
	return out
}

// randomScenario 构造一个确定性（由 seed 决定）的随机压力场景。
func randomScenario(seed int64, tmpDir string) *Scenario {
	rng := rand.New(rand.NewSource(seed))
	sc := &Scenario{
		Name:      fmt.Sprintf("random-%d", seed),
		NodeCount: 3,
		Seed:      seed,
		Tick:      1,
		EndTime:   4000,
		Heartbeat: 40,
		Election:  TimeRange{Min: 150, Max: 350},
		Network: NetworkCfg{
			BaseDelay: int64(3 + rng.Intn(8)),
			JitterPct: roundPct(rng.Float64() * 0.6),
			LossPct:   roundPct(rng.Float64() * 0.3),
			DupPct:    roundPct(rng.Float64() * 0.1),
		},
		DataDir: filepath.Join(tmpDir, fmt.Sprintf("seed%d", seed)),
		Trace:   false,
	}

	dead := map[int]bool{}
	t := int64(400)
	clientSeq := 0
	for t < sc.EndTime-400 {
		t += int64(20 + rng.Intn(260))
		switch rng.Intn(3) {
		case 0: // 客户端写入：随机选节点（可能是跟随者或已死节点）
			clientSeq++
			target := 1 + rng.Intn(3)
			var ref NodeRef
			if rng.Intn(2) == 0 {
				ref = NodeRef{Leader: true}
			} else {
				ref = NodeRef{ID: target}
			}
			sc.Events = append(sc.Events, ScenarioEvent{
				Time: t, Type: "client", Node: &ref,
				Client:  fmt.Sprintf("w%d", clientSeq),
				Command: fmt.Sprintf("cmd-%d", clientSeq),
			})
		case 1: // 崩溃一个存活节点
			alive := aliveNodes(dead)
			if len(alive) == 0 {
				continue
			}
			id := alive[rng.Intn(len(alive))]
			// 不要让三个节点同时死掉（全死场景由固定用例覆盖）。
			if len(alive) <= 1 {
				continue
			}
			ref := NodeRef{ID: id}
			dead[id] = true
			sc.Events = append(sc.Events, ScenarioEvent{Time: t, Type: "crash", Node: &ref})
			// 稍后重启。
			rt := t + int64(300+rng.Intn(1200))
			if rt < sc.EndTime {
				sc.Events = append(sc.Events, ScenarioEvent{Time: rt, Type: "restart", Node: &ref})
				dead[id] = false
			}
		case 2: // 分区：隔离一个随机节点，之后愈合
			if len(sc.Events) > 0 && sc.Events[len(sc.Events)-1].Type == "partition" {
				continue
			}
			isolated := 1 + rng.Intn(3)
			others := []int{}
			for id := 1; id <= 3; id++ {
				if id != isolated {
					others = append(others, id)
				}
			}
			sc.Events = append(sc.Events, ScenarioEvent{
				Time: t, Type: "partition",
				Groups: [][]int{{isolated}, others},
			})
			ht := t + int64(300+rng.Intn(900))
			if ht < sc.EndTime-100 {
				sc.Events = append(sc.Events, ScenarioEvent{Time: ht, Type: "heal"})
			}
		}
	}
	return sc
}

func aliveNodes(dead map[int]bool) []int {
	var out []int
	for id := 1; id <= 3; id++ {
		if !dead[id] {
			out = append(out, id)
		}
	}
	return out
}

func roundPct(v float64) float64 {
	return float64(int(v*100+0.5)) / 100
}
