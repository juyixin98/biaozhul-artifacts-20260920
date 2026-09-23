package simulator

import "fmt"

const (
	KindTransfer = "transfer"
	KindMarker   = "marker"

	PolicyFIFO       = "fifo"
	PolicyBestEffort = "best_effort"
)

func validate(req *Request) error {
	if len(req.Nodes) == 0 {
		return fmt.Errorf("nodes: 至少需要一个节点")
	}
	nodeSet := map[string]bool{}
	for _, n := range req.Nodes {
		if n.Name == "" {
			return fmt.Errorf("nodes: 节点名不能为空")
		}
		if nodeSet[n.Name] {
			return fmt.Errorf("nodes: 节点名重复 %q", n.Name)
		}
		if n.Balance < 0 {
			return fmt.Errorf("nodes: 节点 %q 初始余额不能为负", n.Name)
		}
		nodeSet[n.Name] = true
	}

	linkSet := map[string]bool{}
	for i, l := range req.Links {
		if !nodeSet[l.From] || !nodeSet[l.To] {
			return fmt.Errorf("links[%d]: from/to 必须是已声明的节点", i)
		}
		if l.From == l.To {
			return fmt.Errorf("links[%d]: 不支持自环链路 %s", i, l.From)
		}
		key := l.From + "->" + l.To
		if linkSet[key] {
			return fmt.Errorf("links[%d]: 链路重复 %s", i, key)
		}
		linkSet[key] = true

		policy := l.Policy
		if policy == "" {
			policy = PolicyFIFO
		}
		if policy != PolicyFIFO && policy != PolicyBestEffort {
			return fmt.Errorf("links[%d]: 未知 policy %q", i, l.Policy)
		}
		if l.Base < 0 {
			return fmt.Errorf("links[%d]: base_delay 不能为负", i)
		}
		if l.Jitter < 0 {
			return fmt.Errorf("links[%d]: jitter 不能为负", i)
		}
		if l.LossPct < 0 || l.LossPct > 100 || l.DupPct < 0 || l.DupPct > 100 {
			return fmt.Errorf("links[%d]: loss_pct/dup_pct 必须在 [0,100]", i)
		}
	}

	if len(req.Events) == 0 {
		return fmt.Errorf("events: 至少需要一个事件")
	}
	markerIDs := map[string]bool{}
	for i, e := range req.Events {
		if e.Tick < 0 {
			return fmt.Errorf("events[%d]: tick 不能为负", i)
		}
		if e.RepeatTo < 0 || e.RepeatEvery < 0 {
			return fmt.Errorf("events[%d]: repeat_to/repeat_every 不能为负", i)
		}
		switch e.Kind {
		case KindTransfer:
			if !nodeSet[e.From] || !nodeSet[e.To] {
				return fmt.Errorf("events[%d]: transfer 的 from/to 未声明", i)
			}
			if !linkSet[e.From+"->"+e.To] {
				return fmt.Errorf("events[%d]: 缺少链路 %s->%s", i, e.From, e.To)
			}
			if e.Amount <= 0 {
				return fmt.Errorf("events[%d]: transfer amount 必须为正数", i)
			}
		case KindMarker:
			if !nodeSet[e.From] {
				return fmt.Errorf("events[%d]: marker 的 from 未声明", i)
			}
			if e.SnapshotID == "" {
				return fmt.Errorf("events[%d]: marker 缺少 snapshot_id", i)
			}
			if e.To != "" {
				return fmt.Errorf("events[%d]: marker 事件不接受 to 字段（由协议自动沿出信道传播）", i)
			}
			key := e.From + "|" + e.SnapshotID
			if markerIDs[key] {
				return fmt.Errorf("events[%d]: 节点 %q 重复发起快照 %q", i, e.From, e.SnapshotID)
			}
			markerIDs[key] = true
		default:
			return fmt.Errorf("events[%d]: 未知 kind %q", i, e.Kind)
		}
	}
	if req.TickLimit < 0 {
		return fmt.Errorf("tick_limit 不能为负")
	}
	return nil
}
