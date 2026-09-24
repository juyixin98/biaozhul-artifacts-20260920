// Package topology 描述集群的物理拓扑：
// 设备（GPU）、每个设备所属的 NUMA 节点、设备显存，以及任意两台
// 设备之间的互联通信代价矩阵（NVLink / PCIe / 跨 NUMA / 跨机）。
//
// 本包只负责数据结构、输入校验和代价矩阵构建，不包含任何放置逻辑，
// 也不会访问真实硬件。
package topology

import (
	"errors"
	"fmt"
	"sort"
)

// 默认通信代价：当调用方没有为某一对设备显式给出 link 时使用。
const (
	// DefaultSameNUMA 同一 NUMA 节点内（典型为同 CPU 下的 PCIe/NVLink）。
	DefaultSameNUMA = 1
	// DefaultCrossNUMA 跨 NUMA 节点（要经过额外的 CPU/互联跳数，代价更高）。
	DefaultCrossNUMA = 10
)

// Device 是一台逻辑 GPU 设备的静态描述。
type Device struct {
	// ID 设备唯一标识，非空字符串。
	ID string `json:"id"`
	// MemoryMB 设备总显存（MB），必须 > 0。
	MemoryMB int `json:"memoryMB"`
	// NUMANode 设备所属 NUMA 节点编号，必须 >= 0。
	NUMANode int `json:"numaNode"`
}

// Link 显式声明两台设备之间的双向通信代价。
// 未声明的设备对按“是否同一 NUMA 节点”回退到默认代价。
type Link struct {
	A    string `json:"a"`
	B    string `json:"b"`
	Cost int    `json:"cost"` // 必须 >= 0
}

// Spec 是集群拓扑的可序列化输入格式，也是 /api/cluster 接口的请求体。
type Spec struct {
	Devices []Device `json:"devices"`
	Links   []Link   `json:"links,omitempty"`
	// DefaultSameNUMACost / DefaultCrossNUMACost 可选，<= 0 时使用内置默认值。
	DefaultSameNUMACost  int `json:"defaultSameNUMACost,omitempty"`
	DefaultCrossNUMACost int `json:"defaultCrossNUMACost,omitempty"`
}

// Cluster 是经过校验、索引化的拓扑，代价以稠密矩阵存放。
type Cluster struct {
	spec       Spec
	index      map[string]int
	cost       [][]int
	sameNUMA   int
	crossNUMA  int
	inputIDs   []string
	sortedIDs  []string
	numaOf     []int
	memoryOfMB []int
}

// Build 校验 Spec 并构建不可变的 Cluster。
func Build(s Spec) (*Cluster, error) {
	if len(s.Devices) == 0 {
		return nil, errors.New("devices 不能为空")
	}
	same, cross := s.DefaultSameNUMACost, s.DefaultCrossNUMACost
	if same <= 0 {
		same = DefaultSameNUMA
	}
	if cross <= 0 {
		cross = DefaultCrossNUMA
	}
	if cross < same {
		return nil, fmt.Errorf("默认跨 NUMA 代价(%d)不应小于同 NUMA 代价(%d)，否则拓扑语义矛盾", cross, same)
	}

	n := len(s.Devices)
	index := make(map[string]int, n)
	ids := make([]string, n)
	for i, d := range s.Devices {
		if d.ID == "" {
			return nil, fmt.Errorf("devices[%d] 的 id 不能为空", i)
		}
		if _, dup := index[d.ID]; dup {
			return nil, fmt.Errorf("设备 id 重复: %q", d.ID)
		}
		if d.MemoryMB <= 0 {
			return nil, fmt.Errorf("设备 %q 的 memoryMB 必须 > 0，实际为 %d", d.ID, d.MemoryMB)
		}
		if d.NUMANode < 0 {
			return nil, fmt.Errorf("设备 %q 的 numaNode 必须 >= 0", d.ID)
		}
		index[d.ID] = i
		ids[i] = d.ID
	}

	mem := make([]int, n)
	numa := make([]int, n)
	for i, d := range s.Devices {
		mem[i] = d.MemoryMB
		numa[i] = d.NUMANode
	}

	// 先用 NUMA 默认代价填充稠密矩阵。
	cost := make([][]int, n)
	for i := range cost {
		cost[i] = make([]int, n)
		for j := 0; j < n; j++ {
			if i == j {
				cost[i][j] = 0
			} else if numa[i] == numa[j] {
				cost[i][j] = same
			} else {
				cost[i][j] = cross
			}
		}
	}

	for i, l := range s.Links {
		if l.A == l.B {
			return nil, fmt.Errorf("links[%d] 不能连接设备自身: %q", i, l.A)
		}
		ia, oka := index[l.A]
		ib, okb := index[l.B]
		if !oka || !okb {
			return nil, fmt.Errorf("links[%d] 引用了不存在的设备: %q <-> %q", i, l.A, l.B)
		}
		if l.Cost < 0 {
			return nil, fmt.Errorf("links[%d] (%s<->%s) 的 cost 必须 >= 0", i, l.A, l.B)
		}
		cost[ia][ib] = l.Cost
		cost[ib][ia] = l.Cost // 链路视为双向对称
	}

	sorted := append([]string(nil), ids...)
	sort.Strings(sorted)

	return &Cluster{
		spec:       s,
		index:      index,
		cost:       cost,
		sameNUMA:   same,
		crossNUMA:  cross,
		inputIDs:   ids,
		sortedIDs:  sorted,
		numaOf:     numa,
		memoryOfMB: mem,
	}, nil
}

// N 返回设备数量。
func (c *Cluster) N() int { return len(c.cost) }

// Index 返回设备 id 到下标的映射；id 不存在时 ok=false。
func (c *Cluster) Index(id string) (int, bool) {
	i, ok := c.index[id]
	return i, ok
}

// DeviceID 返回下标的设备 id（下标对应 Spec.Devices 的输入顺序）。
func (c *Cluster) DeviceID(i int) string { return c.inputIDs[i] }

// IDs 返回按输入顺序排列的全部设备 id。
func (c *Cluster) IDs() []string { return append([]string(nil), c.inputIDs...) }

// SortedIDs 返回字典序排列的设备 id（用于稳定的对外展示）。
func (c *Cluster) SortedIDs() []string { return append([]string(nil), c.sortedIDs...) }

// MemoryMB 返回设备 i 的总显存。
func (c *Cluster) MemoryMB(i int) int { return c.memoryOfMB[i] }

// NUMANode 返回设备 i 所属的 NUMA 节点。
func (c *Cluster) NUMANode(i int) int { return c.numaOf[i] }

// Cost 返回设备 i、j 之间的双向通信代价。
func (c *Cluster) Cost(i, j int) int { return c.cost[i][j] }

// SameNUMADefault 返回生效的同 NUMA 默认代价。
func (c *Cluster) SameNUMADefault() int { return c.sameNUMA }

// CrossNUMADefault 返回生效的跨 NUMA 默认代价。
func (c *Cluster) CrossNUMADefault() int { return c.crossNUMA }

// Spec 返回构建该集群所用的输入（副本）。
func (c *Cluster) Spec() Spec {
	devs := append([]Device(nil), c.spec.Devices...)
	links := append([]Link(nil), c.spec.Links...)
	return Spec{
		Devices:              devs,
		Links:                links,
		DefaultSameNUMACost:  c.spec.DefaultSameNUMACost,
		DefaultCrossNUMACost: c.spec.DefaultCrossNUMACost,
	}
}
