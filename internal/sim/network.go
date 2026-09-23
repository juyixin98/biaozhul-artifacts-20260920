package sim

import (
	"fmt"

	"simsnap/internal/msg"
)

// Policy 描述一条逻辑信道（双向链路按方向独立生效）的投递行为。
type Policy struct {
	MinDelay int64 `json:"min_delay"` // 最小传播时延（逻辑时间单位，>=1）
	MaxDelay int64 `json:"max_delay"` // 最大传播时延（>= MinDelay）
	LossPct  int   `json:"loss_pct"`  // 丢包概率百分比 0..100
	DupPct   int   `json:"dup_pct"`   // 重复投递概率百分比 0..100
	FIFO     bool  `json:"fifo"`      // 是否保证按发送顺序投递
}

// ReliableFIFO 返回 Chandy–Lamport 所要求的可靠 FIFO 信道策略。
func ReliableFIFO(minDelay, maxDelay int64) Policy {
	return Policy{MinDelay: minDelay, MaxDelay: maxDelay, FIFO: true}
}

// Stats 是一条信道方向上的网络统计。
type Stats struct {
	Sent       int `json:"sent"`       // 发送的逻辑消息数（含后被丢弃的）
	Delivered  int `json:"delivered"`  // 实际投递数（含重复副本）
	Lost       int `json:"lost"`       // 丢弃数
	Duplicated int `json:"duplicated"` // 多投出的重复副本数
	Reordered  int `json:"reordered"`  // 乱序投递次数
}

// Delivery 是一次实际投递携带的信息。
type Delivery struct {
	Msg    msg.Message // 投递的消息（重复副本共享同一 SendID）
	SendID int         // 发送方在该方向信道上的单调编号
	At     int64       // 投递时刻
	Dup    bool        // 是否为重复副本
}

type link struct {
	policy      Policy
	lastSent    int
	lastDeliv   int
	nextArrival int64 // FIFO 约束：下一个允许的最早投递时刻
	stats       Stats
}

func linkKey(a, b int) string {
	if a > b {
		a, b = b, a
	}
	return fmt.Sprintf("%d-%d", a, b)
}

// Network 是节点集合之上的消息网络：按链路策略模拟延迟、丢包、重复与乱序。
// 标记消息与业务消息走同一条信道——这是 Chandy–Lamport 算法的前提。
type Network struct {
	eng   *Engine
	links map[string]*link
}

// NewNetwork 创建网络。
func NewNetwork(eng *Engine) *Network {
	return &Network{eng: eng, links: map[string]*link{}}
}

// AddLink 在 a、b 之间按 policy 建立逻辑链路（两个方向使用同一份策略）。
func (n *Network) AddLink(a, b int, policy Policy) {
	n.links[linkKey(a, b)] = &link{policy: policy}
}

// Send 在 m.From -> m.To 方向发送一条逻辑消息。
// 返回填好 MsgID/SentAt 的消息（调用方应以返回值为准）；丢弃时编号仍占用。
// onDeliver 在每个实际投递的副本（可能重复）发生时调用。
func (n *Network) Send(m msg.Message, onDeliver func(Delivery)) msg.Message {
	l := n.links[linkKey(m.From, m.To)]
	if l == nil {
		panic(fmt.Sprintf("sim: 节点 %d 与 %d 之间不存在链路", m.From, m.To))
	}

	l.lastSent++
	m.MsgID = l.lastSent
	m.SentAt = n.eng.Now()
	l.stats.Sent++

	lost := l.policy.LossPct > 0 && n.eng.RNG().Intn(100) < l.policy.LossPct
	if lost {
		l.stats.Lost++
		return m
	}

	duplicate := l.policy.DupPct > 0 && n.eng.RNG().Intn(100) < l.policy.DupPct

	span := l.policy.MaxDelay - l.policy.MinDelay + 1
	delay := l.policy.MinDelay
	if span > 1 {
		delay += n.eng.RNG().Int63n(span)
	}
	at := n.eng.Now() + delay
	if l.policy.FIFO && at < l.nextArrival {
		at = l.nextArrival
	}
	l.nextArrival = at + 1

	n.schedule(l, m, at, false, onDeliver)
	if duplicate {
		dat := at + 1
		if l.policy.FIFO {
			dat = l.nextArrival
		}
		l.nextArrival = dat + 1
		n.schedule(l, m, dat, true, onDeliver)
	}
	return m
}

func (n *Network) schedule(l *link, m msg.Message, at int64, dup bool, onDeliver func(Delivery)) {
	n.eng.ScheduleAt(at, 1, func(now int64) {
		l.stats.Delivered++
		if dup {
			l.stats.Duplicated++
		}
		if m.MsgID < l.lastDeliv {
			l.stats.Reordered++
		}
		l.lastDeliv = m.MsgID
		onDeliver(Delivery{Msg: m, SendID: m.MsgID, At: now, Dup: dup})
	})
}

// Stats 返回 a->b 方向的链路统计副本。
func (n *Network) Stats(a, b int) Stats {
	if l := n.links[linkKey(a, b)]; l != nil {
		return l.stats
	}
	return Stats{}
}

// PolicyOf 返回 a、b 之间链路的策略副本。
func (n *Network) PolicyOf(a, b int) Policy {
	if l := n.links[linkKey(a, b)]; l != nil {
		return l.policy
	}
	return Policy{}
}
