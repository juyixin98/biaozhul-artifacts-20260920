// Package route 在“只经过充电点”的图上做 Pareto 标签搜索，
// 给出从机器人当前状态(位置/时间/电量)出发，到达某个目标点(任务点)
// 且到达电量不低于指定值的所有非劣路径方案。
//
// 一个方案描述一串经过的点，其中某些点是充满电的充电点。
// 方案之间按 (到达时间, 到达电量) 比较：若方案 A 的到达时间不晚于 B、
// 到达电量不低于 B，且至少一项严格更优，则 A 支配 B。
package route

import (
	"github.com/example/robot-task/internal/model"
)

// Hop 表示路径上经过的一个点。
type Hop struct {
	Point     model.Point
	IsCharger bool
	ChargerID string
	Charged   bool // 仅在充电点充满电时为 true(途经未充为 false)
}

// Option 是一条非劣路径方案。
type Option struct {
	Path           []Hop
	ArrivalTime    int64 // 到达目标点的时间
	ArrivalBattery int64 // 到达目标点时的剩余电量
}

type label struct {
	point     model.Point
	isCharger bool
	chargerID string
	t         int64 // 到达(并已完成可能的充电)后的时间
	battery   int64 // 到达(并已完成可能的充电)后的电量
	path      []Hop
}

// ReachOptions 返回所有可行的非劣路径方案。
//
//   - from, time, battery: 机器人当前位置、时间、电量
//   - target: 目标点(任务位置)
//   - maxBattery: 电池容量
//   - energyPerDist: 单位距离耗能
//   - chargeRate: 充 1 单位电量所需时间
//   - deadline: 到达目标点的时间不得晚于该值(超过则裁剪)
//   - arriveBatteryMin: 到达目标点时电量下限
//   - chargers: 全部充电点
func ReachOptions(
	from model.Point,
	time, battery int64,
	target model.Point,
	maxBattery, energyPerDist, chargeRate, deadline, arriveBatteryMin int64,
	chargers []model.Charger,
) []Option {
	// 图节点：起点索引 0，目标点索引 1，其后为充电点。
	// 起点或目标点本身可能与某充电点重合，用单独索引处理也正确。
	nodes := []Hop{{Point: from}, {Point: target}}
	for _, c := range chargers {
		nodes = append(nodes, Hop{Point: c.Location, IsCharger: true, ChargerID: c.ID})
	}

	// labels[i] 是节点 i 上尚未被支配的标签集合。
	labels := make([][]label, len(nodes))
	labels[0] = []label{{
		point:   from,
		t:       time,
		battery: battery,
		path:    []Hop{{Point: from}},
	}}

	const maxLabelsTotal = 50000
	total := 1

	// 用栈做工作清单：每次弹出一个标签，向所有其他节点扩展。
	stack := []struct{ node, idx int }{{0, 0}}
	for len(stack) > 0 {
		top := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		n, li := top.node, top.idx
		// 目标点是汇点：到达后不再离开(离开再回来只会更晚)。
		if n == 1 {
			continue
		}
		// 标签可能在入栈后被新标签支配掉。
		if li >= len(labels[n]) {
			continue
		}
		cur := labels[n][li]

		for j := range nodes {
			if j == n {
				continue
			}
			d := model.Dist(cur.point, nodes[j].Point)
			cost := d * energyPerDist
			if cost > cur.battery {
				continue // 电量不足以直接开过去
			}
			arrive := cur.t + d
			rem := cur.battery - cost
			if arrive > deadline {
				continue
			}

			// 生成候选标签。
			candidates := []label{{
				point:     nodes[j].Point,
				isCharger: nodes[j].IsCharger,
				chargerID: nodes[j].ChargerID,
				t:         arrive,
				battery:   rem,
				path:      appendPath(cur.path, nodes[j]),
			}}
			// 若到达的是充电点，再生成一个“充满电”标签。
			if nodes[j].IsCharger && rem < maxBattery {
				chargedHop := nodes[j]
				chargedHop.Charged = true
				candidates = append(candidates, label{
					point:     nodes[j].Point,
					isCharger: true,
					chargerID: nodes[j].ChargerID,
					t:         arrive + (maxBattery-rem)*chargeRate,
					battery:   maxBattery,
					path:      appendPath(cur.path, chargedHop),
				})
			}

			for _, cand := range candidates {
				if cand.t > deadline {
					continue
				}
				// 目标点要求到达电量不低于下限。
				if j == 1 && cand.battery < arriveBatteryMin {
					continue
				}
				// 支配检查。
				dominated := false
				kept := labels[j][:0]
				for _, old := range labels[j] {
					if dominates(old, cand) || (old.t == cand.t && old.battery == cand.battery) {
						dominated = true
					}
					// cand 支配 old 则丢弃 old。
					if !dominates(cand, old) {
						kept = append(kept, old)
					}
				}
				if dominated {
					continue
				}
				kept = append(kept, cand)
				labels[j] = kept
				total++
				if total > maxLabelsTotal {
					// 标签数异常膨胀(病态多充电点输入)时停止扩展，
					// 返回目前已有的目标点标签。
					return collectOptions(labels[1])
				}
				stack = append(stack, struct{ node, idx int }{j, len(labels[j]) - 1})
			}
		}
	}

	return collectOptions(labels[1])
}

func appendPath(p []Hop, h Hop) []Hop {
	out := make([]Hop, 0, len(p)+1)
	out = append(out, p...)
	out = append(out, h)
	return out
}

// dominates 报告 a 是否支配 b(a 时间不晚、电量不低，且一项严格更优)。
func dominates(a, b label) bool {
	return a.t <= b.t && a.battery >= b.battery && (a.t < b.t || a.battery > b.battery)
}

func collectOptions(ls []label) []Option {
	opts := make([]Option, 0, len(ls))
	for _, l := range ls {
		opts = append(opts, Option{
			Path:           l.path,
			ArrivalTime:    l.t,
			ArrivalBattery: l.battery,
		})
	}
	return opts
}
