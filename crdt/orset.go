// Package crdt 实现 Observed-Remove Set（OR-Set / OR-Map 集合语义）。
//
// 状态定义（状态型 CRDT，state-based）：
//
//	A: value -> 已观察到的添加标签集合（唯一标签）
//	R: value -> 已观察到的删除墓碑标签集合
//
// 有效元素 = { v | A[v] - R[v] 非空 }。
//
// 合并（join）为按标签集合的逐项并集：A' = A∪A_other，R' = R∪R_other。
// 并集运算天然满足交换律、结合律、幂等律，因此合并结果与消息到达顺序、
// 重复到达次数无关，这是模拟器中网络可乱序、可重复的正确性基础。
package crdt

import (
	"fmt"
	"sort"
)

// UniqueTag 是一次 Add 的全局唯一标签。
// Origin 为发起副本标识，Counter 为该副本单调递增的本地序号。
// 约定：标签一旦分配给某次 Add 就永不复用（即使该标签已被墓碑化）。
type UniqueTag struct {
	Origin  string `json:"origin"`
	Counter uint64 `json:"counter"`
}

// String 返回可读形式 "origin#counter"。
func (t UniqueTag) String() string {
	return fmt.Sprintf("%s#%d", t.Origin, t.Counter)
}

// compareTag 定义标签的全序：先按 Origin 字典序，再按 Counter。
func compareTag(a, b UniqueTag) int {
	if a.Origin < b.Origin {
		return -1
	}
	if a.Origin > b.Origin {
		return 1
	}
	if a.Counter < b.Counter {
		return -1
	}
	if a.Counter > b.Counter {
		return 1
	}
	return 0
}

// ORSet 是观察删除集合。零值不可用，请使用 New 构造。
type ORSet struct {
	// A、R 的标签切片始终保持升序、去重，以便做线性归并与确定性序列化。
	A map[string][]UniqueTag
	R map[string][]UniqueTag
}

// New 返回空 OR-Set。
func New() *ORSet {
	return &ORSet{
		A: make(map[string][]UniqueTag),
		R: make(map[string][]UniqueTag),
	}
}

// insertSortedUnique 向升序切片插入一个标签（去重），返回新切片与是否实际插入。
func insertSortedUnique(s []UniqueTag, t UniqueTag) ([]UniqueTag, bool) {
	i := sort.Search(len(s), func(i int) bool { return compareTag(s[i], t) >= 0 })
	if i < len(s) && compareTag(s[i], t) == 0 {
		return s, false
	}
	s = append(s, UniqueTag{})
	copy(s[i+1:], s[i:])
	s[i] = t
	return s, true
}

// containsTag 在升序切片中二分查找标签。
func containsTag(s []UniqueTag, t UniqueTag) bool {
	i := sort.Search(len(s), func(i int) bool { return compareTag(s[i], t) >= 0 })
	return i < len(s) && compareTag(s[i], t) == 0
}

// unionSorted 归并两个升序去重切片，返回其并集。
func unionSorted(a, b []UniqueTag) []UniqueTag {
	out := make([]UniqueTag, 0, len(a)+len(b))
	i, j := 0, 0
	for i < len(a) || j < len(b) {
		var t UniqueTag
		switch {
		case j == len(b):
			t = a[i]
			i++
		case i == len(a):
			t = b[j]
			j++
		case compareTag(a[i], b[j]) < 0:
			t = a[i]
			i++
		case compareTag(a[i], b[j]) > 0:
			t = b[j]
			j++
		default:
			t = a[i]
			i++
			j++
		}
		if len(out) == 0 || compareTag(out[len(out)-1], t) != 0 {
			out = append(out, t)
		}
	}
	return out
}

// tagSeenAnywhere 报告标签是否已存在于任意元素的 A 或 R 中。
// 唯一标签一旦历史上出现过就不可复用：复用一个已在 R 中的标签会让新值
// 立即被当作已删除；复用 A 中标签会让两个值共享同一条添加事实。
func (s *ORSet) tagSeenAnywhere(t UniqueTag) bool {
	for _, tags := range s.A {
		if containsTag(tags, t) {
			return true
		}
	}
	for _, tags := range s.R {
		if containsTag(tags, t) {
			return true
		}
	}
	return false
}

// AddWithTag 使用调用方指定的唯一标签添加元素。
// 标签在本副本已观察的全部历史（A∪R）中必须从未出现过，否则返回错误。
func (s *ORSet) AddWithTag(value string, tag UniqueTag) error {
	if value == "" {
		return fmt.Errorf("crdt: 元素值不能为空")
	}
	if s.tagSeenAnywhere(tag) {
		return fmt.Errorf("crdt: 标签 %s 已被使用，唯一标签不可复用", tag)
	}
	s.A[value], _ = insertSortedUnique(s.A[value], tag)
	return nil
}

// Add 是便捷方法：以 (origin, counter) 构造标签并添加。
func (s *ORSet) Add(value, origin string, counter uint64) (UniqueTag, error) {
	tag := UniqueTag{Origin: origin, Counter: counter}
	if err := s.AddWithTag(value, tag); err != nil {
		return UniqueTag{}, err
	}
	return tag, nil
}

// RemoveObserved 执行“观察删除”：只把当前在本副本 A[value] 中已观察到、
// 且尚不在 R 中的存活标签写入墓碑集合。
//
// 关键语义：对尚未观察到的并发 Add 标签一无所知，因此不会、也无法删除它们；
// 那些并发标签之后通过合并到达时仍然存活。
// 返回被墓碑化的标签；元素不存在或已无效时返回 false。
func (s *ORSet) RemoveObserved(value string) ([]UniqueTag, bool) {
	live := s.LiveTags(value)
	if len(live) == 0 {
		return nil, false
	}
	s.R[value] = unionSorted(s.R[value], live)
	return live, true
}

// LiveTags 返回 A[value] - R[value]（当前支撑该元素存活的标签）。
func (s *ORSet) LiveTags(value string) []UniqueTag {
	a, r := s.A[value], s.R[value]
	live := make([]UniqueTag, 0, len(a))
	for _, t := range a {
		if !containsTag(r, t) {
			live = append(live, t)
		}
	}
	return live
}

// Merge 将 other 的状态并入接收者（join = 标签集合并集）。
// 该操作：
//   - 交换：s.Merge(o) 的有效状态 == o.Merge(s)
//   - 结合：(s.Merge(a)).Merge(b) == s.Merge(a.Merge(b))
//   - 幂等：重复 Merge 同一个状态不产生任何变化
//
// Merge 会就地修改接收者；other 不会被修改。
func (s *ORSet) Merge(other *ORSet) {
	if other == nil {
		return
	}
	seen := make(map[string]struct{}, len(s.A)+len(other.A)+len(s.R)+len(other.R))
	for v := range s.A {
		seen[v] = struct{}{}
	}
	for v := range other.A {
		seen[v] = struct{}{}
	}
	for v := range s.R {
		seen[v] = struct{}{}
	}
	for v := range other.R {
		seen[v] = struct{}{}
	}
	for v := range seen {
		if a := unionSorted(s.A[v], other.A[v]); len(a) > 0 {
			s.A[v] = a
		}
		if r := unionSorted(s.R[v], other.R[v]); len(r) > 0 {
			s.R[v] = r
		}
	}
}

// Contains 报告元素当前是否在集合中。
func (s *ORSet) Contains(value string) bool {
	return len(s.LiveTags(value)) > 0
}

// Values 返回全部有效元素，字典序升序，保证确定性。
func (s *ORSet) Values() []string {
	out := make([]string, 0, len(s.A))
	for v := range s.A {
		if len(s.LiveTags(v)) > 0 {
			out = append(out, v)
		}
	}
	sort.Strings(out)
	return out
}

// Clone 返回深拷贝。
func (s *ORSet) Clone() *ORSet {
	c := New()
	for v, tags := range s.A {
		c.A[v] = append([]UniqueTag(nil), tags...)
	}
	for v, tags := range s.R {
		c.R[v] = append([]UniqueTag(nil), tags...)
	}
	return c
}

// Equal 比较两个状态的 A、R 标签集合是否完全相同（不仅是有效值，
// 还包括墓碑——模拟器用它判断“状态级收敛”，区别于仅有效值收敛）。
func (s *ORSet) Equal(other *ORSet) bool {
	if other == nil {
		return false
	}
	return tagsMapsEqual(s.A, other.A) && tagsMapsEqual(s.R, other.R)
}

func tagsMapsEqual(a, b map[string][]UniqueTag) bool {
	if len(a) != len(b) {
		return false
	}
	for v, tags := range a {
		other, ok := b[v]
		if !ok || len(tags) != len(other) {
			return false
		}
		for i := range tags {
			if compareTag(tags[i], other[i]) != 0 {
				return false
			}
		}
	}
	return true
}

// Export 将标签转成可读字符串（"origin#counter"），用于 JSON 输出与检查。
// 返回 A、R 两份映射；空映射也非 nil，便于稳定序列化。
func (s *ORSet) Export() (a, r map[string][]string) {
	a = make(map[string][]string)
	r = make(map[string][]string)
	for v, tags := range s.A {
		out := make([]string, 0, len(tags))
		for _, t := range tags {
			out = append(out, t.String())
		}
		sort.Strings(out)
		a[v] = out
	}
	for v, tags := range s.R {
		out := make([]string, 0, len(tags))
		for _, t := range tags {
			out = append(out, t.String())
		}
		sort.Strings(out)
		r[v] = out
	}
	return a, r
}

// Signature 返回状态的规范字符串指纹，供模拟器判断状态是否变化/重复。
func (s *ORSet) Signature() string {
	values := make([]string, 0, len(s.A)+len(s.R))
	seen := make(map[string]struct{}, len(s.A)+len(s.R))
	for v := range s.A {
		if _, ok := seen[v]; !ok {
			seen[v] = struct{}{}
			values = append(values, v)
		}
	}
	for v := range s.R {
		if _, ok := seen[v]; !ok {
			seen[v] = struct{}{}
			values = append(values, v)
		}
	}
	sort.Strings(values)
	b := make([]byte, 0, 256)
	for _, v := range values {
		b = append(b, 'A')
		for _, t := range s.A[v] {
			b = append(b, ' ')
			b = append(b, t.String()...)
		}
		b = append(b, '|', 'R')
		for _, t := range s.R[v] {
			b = append(b, ' ')
			b = append(b, t.String()...)
		}
		b = append(b, ';', ' ')
		b = append(b, v...)
	}
	return string(b)
}

// SafeToReclaim 判断墓碑回收的稳定性前提是否成立：
// 接收者持有的每一个墓碑标签，都必须已经被所有将参与合并的副本观察到
// （即在它们的 R 中）。others 必须枚举“现在或将来可能与本副本合并”的
// 全部副本——遗漏一个长期分区或退役后又回归的副本，回收就不安全。
//
// 额外前提（由标签分配策略保证，非运行时可检查）：标签全局唯一且永不复用。
func (s *ORSet) SafeToReclaim(others ...*ORSet) bool {
	for _, tombs := range s.R {
		for _, t := range tombs {
			// 墓碑标签必须已被每个将参与合并的副本观察（在其 R 任意元素下出现）。
			for _, o := range others {
				if o == nil || !tagInMap(o.R, t) {
					return false
				}
			}
		}
	}
	return true
}

// tagInMap 报告标签是否出现在 map 的任意键下。
func tagInMap(m map[string][]UniqueTag, t UniqueTag) bool {
	for _, tags := range m {
		if containsTag(tags, t) {
			return true
		}
	}
	return false
}

// ReclaimGC 在调用方确认稳定性前提（见 SafeToReclaim）后回收墓碑：
// 从 R 中删除墓碑标签，并从 A 中删除同一批标签（它们已无贡献）。
//
// 安全性：对所有已观察该墓碑的副本，回收前后有效元素集合不变，互相合并
// 仍然收敛。风险：若一个未观察墓碑的滞后副本仍持有该标签的 A 记录，
// 与之合并会把标签重新当作存活添加事实——元素“复活”。因此必须先用
// SafeToReclaim 对完整副本集合确认，且未来标签依旧不得复用。
// 返回回收的墓碑标签数量。
func (s *ORSet) ReclaimGC() int {
	n := 0
	for v, tombs := range s.R {
		if len(tombs) == 0 {
			continue
		}
		n += len(tombs)
		a := s.A[v]
		for _, t := range tombs {
			if i := sort.Search(len(a), func(i int) bool { return compareTag(a[i], t) >= 0 }); i < len(a) && compareTag(a[i], t) == 0 {
				a = append(a[:i], a[i+1:]...)
			}
		}
		if len(a) > 0 {
			s.A[v] = a
		} else {
			delete(s.A, v)
		}
		delete(s.R, v)
	}
	return n
}
