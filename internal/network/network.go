// Package network 模拟节点之间的网络：每条消息在未来某个 tick 投递，
// 可配置固定延迟、随机抖动以及丢包、重复、乱序。
//
// 乱序的实现：每条消息的投递 tick 在 [delay, delay+jitter) 中随机抽取，
// 消息按 (tick, 序号) 严格排序投递，因此先发可能后到。
// 所有随机数来自模拟器注入的确定性 rand.Source，保证同脚本可复现。
package network

import (
	"math/rand"

	"raftdemo/internal/raft"
)

// pending 是网络中正在传输的消息。
type pending struct {
	deliverAt int
	seq       int
	msg       raft.Msg
}

// Link 描述单向链路（from->to）的故障参数。
type Link struct {
	DelayTicks int     `json:"delay_ticks"`
	Jitter     int     `json:"jitter"`
	LossRate   float64 `json:"loss_rate"` // [0,1)：直接丢弃的概率
	DupRate    float64 `json:"dup_rate"`  // [0,1)：额外复制一份的概率
}

// Network 维护全部链路状态与在途消息。
type Network struct {
	n       int
	links   map[[2]int]*Link
	rng     *rand.Rand
	queue   []pending
	seq     int
	dropped int
	dup     int
}

// New 创建网络并写入默认链路（固定 1 tick 延迟、无故障）。
func New(n int, rng *rand.Rand) *Network {
	nw := &Network{n: n, links: map[[2]int]*Link{}, rng: rng}
	for i := 0; i < n; i++ {
		for j := 0; j < n; j++ {
			if i == j {
				continue
			}
			nw.links[[2]int{i, j}] = &Link{DelayTicks: 1}
		}
	}
	return nw
}

// SetLink 更新单向链路参数（nil 表示恢复默认）。
func (nw *Network) SetLink(from, to int, l *Link) {
	key := [2]int{from, to}
	if l == nil {
		nw.links[key] = &Link{DelayTicks: 1}
		return
	}
	nw.links[key] = l
}

// SetPartition 按分区集合施加全断/恢复。
// partition[i] 是节点 i 的分区号；不同分区间的双向链路丢包率置为 1（隔离）。
// heal=true 时把这些链路恢复为默认。
func (nw *Network) SetPartition(partition []int, heal bool) {
	for i := 0; i < nw.n; i++ {
		for j := 0; j < nw.n; j++ {
			if i == j {
				continue
			}
			if partition[i] != partition[j] {
				if heal {
					nw.SetLink(i, j, nil)
				} else {
					nw.SetLink(i, j, &Link{DelayTicks: 1, LossRate: 1})
				}
			}
		}
	}
}

// ResetAll 把全部单向链路恢复为默认参数（1 tick 延迟、无故障）。
func (nw *Network) ResetAll() {
	for i := 0; i < nw.n; i++ {
		for j := 0; j < nw.n; j++ {
			if i == j {
				continue
			}
			nw.links[[2]int{i, j}] = &Link{DelayTicks: 1}
		}
	}
}

// Isolate 设置某节点与其余所有节点之间的双向隔离。
func (nw *Network) Isolate(id int, heal bool) {
	for j := 0; j < nw.n; j++ {
		if j == id {
			continue
		}
		if heal {
			nw.SetLink(id, j, nil)
			nw.SetLink(j, id, nil)
		} else {
			nw.SetLink(id, j, &Link{DelayTicks: 1, LossRate: 1})
			nw.SetLink(j, id, &Link{DelayTicks: 1, LossRate: 1})
		}
	}
}

// Send 在 now tick 发送一条消息，按链路参数决定丢弃、重复与投递时间。
func (nw *Network) Send(now int, m raft.Msg) {
	link := nw.links[[2]int{m.From, m.To}]
	if link == nil {
		link = &Link{DelayTicks: 1}
	}
	if link.LossRate > 0 && nw.rng.Float64() < link.LossRate {
		nw.dropped++
		return
	}
	delay := link.DelayTicks
	if link.Jitter > 0 {
		delay += nw.rng.Intn(link.Jitter)
	}
	if delay < 1 {
		delay = 1
	}
	nw.enqueue(now+delay, m)
	if link.DupRate > 0 && nw.rng.Float64() < link.DupRate {
		nw.dup++
		// 副本延迟独立抖动，制造重复+乱序。
		d2 := link.DelayTicks
		if link.Jitter > 0 {
			d2 += nw.rng.Intn(link.Jitter)
		}
		if d2 < 1 {
			d2 = 1
		}
		nw.enqueue(now+d2, m)
	}
}

func (nw *Network) enqueue(at int, m raft.Msg) {
	nw.seq++
	nw.queue = append(nw.queue, pending{deliverAt: at, seq: nw.seq, msg: m})
}

// Tick 返回 now tick 应当投递的全部消息，按序号确定顺序。
func (nw *Network) Tick(now int) []raft.Msg {
	var out []raft.Msg
	rest := nw.queue[:0]
	for _, p := range nw.queue {
		if p.deliverAt == now {
			out = append(out, p.msg)
		} else {
			rest = append(rest, p)
		}
	}
	nw.queue = rest
	// out 按 queue 的追加顺序生成，而 queue 按全局发送序号追加，
	// 因此同 tick 内投递顺序天然确定，无需再排序。
	return out
}

// Pending 返回在途消息数。
func (nw *Network) Pending() int { return len(nw.queue) }

// Dropped 返回累计丢弃消息数。
func (nw *Network) Dropped() int { return nw.dropped }

// Duplicated 返回累计重复投递份数。
func (nw *Network) Duplicated() int { return nw.dup }
