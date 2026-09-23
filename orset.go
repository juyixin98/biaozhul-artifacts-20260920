package main

import (
	"fmt"
	"sort"
	"sync"
)

// Tag 唯一标识一次 Add 操作。由「副本 ID + 单调序号」构成，
// 因此不同副本、同一副本不同时刻产生的标签必然不同。
type Tag string

// State 是 OR-Set 的可序列化状态：
//   - Adds: 元素 -> 该元素上曾被观察到的所有 add 标签集合
//   - Rems: 元素 -> 已被删除的标签集合（墓碑 tombstone）
//
// 元素当前「存活」的标签 = Adds[e] - Rems[e]，只要还有一个存活标签，
// 元素就属于集合。
type State struct {
	Adds map[string]map[Tag]bool `json:"adds"`
	Rems map[string]map[Tag]bool `json:"rems"`
}

// ORSet 是 Observed-Remove Set（观测删除集合）CRDT。
// 合并语义为逐集合取并集，满足交换律、结合律、幂等律，
// 因此支持离线副本与任意消息重排、重复投递，最终必然收敛。
type ORSet struct {
	mu   sync.Mutex
	id   string // 副本 ID，用于生成唯一标签
	seq  uint64
	adds map[string]map[Tag]bool
	rems map[string]map[Tag]bool
}

func NewORSet(replicaID string) *ORSet {
	return &ORSet{
		id:   replicaID,
		adds: make(map[string]map[Tag]bool),
		rems: make(map[string]map[Tag]bool),
	}
}

// Add 为元素生成一个全局唯一标签并记录。返回该标签。
func (s *ORSet) Add(element string) Tag {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.seq++
	tag := Tag(fmt.Sprintf("%s#%d", s.id, s.seq))
	if s.adds[element] == nil {
		s.adds[element] = make(map[Tag]bool)
	}
	s.adds[element][tag] = true
	return tag
}

// Remove 只把「当前已观察到」的标签移入墓碑集合。
// 关键点：其他副本并发产生的、本副本尚未见到的标签不受影响，
// 合并后这些标签仍然存活 —— 这就是 "observed-remove" 的含义。
// 返回本次删除移除的标签列表。
func (s *ORSet) Remove(element string) []Tag {
	s.mu.Lock()
	defer s.mu.Unlock()
	var removed []Tag
	for tag := range s.adds[element] {
		if s.rems[element] == nil {
			s.rems[element] = make(map[Tag]bool)
		}
		s.rems[element][tag] = true
		removed = append(removed, tag)
	}
	sort.Slice(removed, func(i, j int) bool { return removed[i] < removed[j] })
	return removed
}

// Contains 判断元素是否在集合中（存在至少一个未被删除的标签）。
func (s *ORSet) Contains(element string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.containsLocked(element)
}

func (s *ORSet) containsLocked(element string) bool {
	for tag := range s.adds[element] {
		if !s.rems[element][tag] {
			return true
		}
	}
	return false
}

// Elements 返回当前存活元素的有序列表。
func (s *ORSet) Elements() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []string
	for e := range s.adds {
		if s.containsLocked(e) {
			out = append(out, e)
		}
	}
	sort.Strings(out)
	return out
}

// Snapshot 返回状态的深拷贝，可安全用于 JSON 序列化与网络传输。
func (s *ORSet) Snapshot() State {
	s.mu.Lock()
	defer s.mu.Unlock()
	return State{
		Adds: cloneMap(s.adds),
		Rems: cloneMap(s.rems),
	}
}

// Merge 将另一份状态并入本副本：adds 与 rems 分别取并集。
// 并集运算天然满足交换、结合、幂等，因此：
//   - 消息可以任意重排（交换律 + 结合律）
//   - 消息可以重复投递（幂等律）
//   - 离线副本重新上线后任意顺序同步都能收敛到同一状态
func (s *ORSet) Merge(other State) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for e, tags := range other.Adds {
		if s.adds[e] == nil {
			s.adds[e] = make(map[Tag]bool)
		}
		for t := range tags {
			s.adds[e][t] = true
		}
	}
	for e, tags := range other.Rems {
		if s.rems[e] == nil {
			s.rems[e] = make(map[Tag]bool)
		}
		for t := range tags {
			s.rems[e][t] = true
		}
	}
}

// Equal 比较两个状态是否包含完全相同的标签集合（用于收敛性断言）。
func Equal(a, b State) bool {
	return mapEqual(a.Adds, b.Adds) && mapEqual(a.Rems, b.Rems)
}

func cloneMap(m map[string]map[Tag]bool) map[string]map[Tag]bool {
	out := make(map[string]map[Tag]bool, len(m))
	for e, tags := range m {
		cp := make(map[Tag]bool, len(tags))
		for t := range tags {
			cp[t] = true
		}
		out[e] = cp
	}
	return out
}

func mapEqual(a, b map[string]map[Tag]bool) bool {
	if len(a) != len(b) {
		return false
	}
	for e, tagsA := range a {
		tagsB, ok := b[e]
		if !ok || len(tagsA) != len(tagsB) {
			return false
		}
		for t := range tagsA {
			if !tagsB[t] {
				return false
			}
		}
	}
	return true
}
