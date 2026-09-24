// Package topology defines the cluster topology model: devices with memory
// capacity and NUMA affinity, plus interconnect link costs between devices.
//
// The topology does not model real GPUs. It is a pure data model used by the
// placement solver.
package topology

import (
	"fmt"
	"sort"
)

// Device is a schedulable device (e.g. a GPU) in the cluster.
//
// MemoryPerReplica required by a task must fit in FreeMemory; the solver never
// over-commits memory. UsedMemory is purely descriptive (the service is
// stateless and does not track allocations across requests).
type Device struct {
	ID         string
	MemoryMB   int64 // total memory capacity
	UsedMemory int64 // memory already consumed by other workloads
	NumaNode   int
}

// FreeMemory returns the allocatable memory of the device.
func (d Device) FreeMemory() int64 {
	return d.MemoryMB - d.UsedMemory
}

// Link declares the interconnect cost of a single undirected device pair.
// Cost is measured in arbitrary units (e.g. latency or a weighted hop count):
// lower is better. The matrix is symmetric.
type Link struct {
	A    string
	B    string
	Cost int64
}

// Cluster is the full topology snapshot for one placement request.
type Cluster struct {
	Devices []Device
	Links   []Link

	// DefaultSameNuma / DefaultCrossNuma are used to fill in pairs for which
	// no explicit Link was provided. They must be non-negative.
	DefaultSameNuma  int64
	DefaultCrossNuma int64
}

// DeviceAt returns the device with the given ID and true, or false.
func (c *Cluster) DeviceAt(id string) (Device, bool) {
	for _, d := range c.Devices {
		if d.ID == id {
			return d, true
		}
	}
	return Device{}, false
}

// BuildCostMatrix validates the topology and returns:
//
//   - order: device IDs sorted ascending, defining the index space
//   - index: ID -> index lookup
//   - matrix: symmetric NxN cost matrix; matrix[i][i] == 0
//
// Missing pairs are filled with DefaultSameNuma / DefaultCrossNuma. Explicit
// links must be consistent (a pair declared twice with conflicting costs is an
// error) and cannot be self-loops or reference unknown devices.
func (c *Cluster) BuildCostMatrix() (order []string, index map[string]int, matrix [][]int64, err error) {
	n := len(c.Devices)
	if n == 0 {
		return nil, nil, nil, fmt.Errorf("cluster contains no devices")
	}
	if c.DefaultSameNuma < 0 || c.DefaultCrossNuma < 0 {
		return nil, nil, nil, fmt.Errorf("default link costs must be non-negative")
	}

	seen := make(map[string]int, n)
	for i, d := range c.Devices {
		if d.ID == "" {
			return nil, nil, nil, fmt.Errorf("device at position %d has an empty ID", i)
		}
		if _, dup := seen[d.ID]; dup {
			return nil, nil, nil, fmt.Errorf("duplicate device ID %q", d.ID)
		}
		if d.MemoryMB < 0 {
			return nil, nil, nil, fmt.Errorf("device %q has negative memory capacity", d.ID)
		}
		if d.UsedMemory < 0 {
			return nil, nil, nil, fmt.Errorf("device %q has negative used memory", d.ID)
		}
		if d.UsedMemory > d.MemoryMB {
			return nil, nil, nil, fmt.Errorf("device %q used memory (%d) exceeds capacity (%d)", d.ID, d.UsedMemory, d.MemoryMB)
		}
		if d.NumaNode < 0 {
			return nil, nil, nil, fmt.Errorf("device %q has negative NUMA node", d.ID)
		}
		seen[d.ID] = i
	}

	order = make([]string, n)
	copy(order, deviceIDs(c.Devices))
	sort.Strings(order)
	index = make(map[string]int, n)
	for i, id := range order {
		index[id] = i
	}

	matrix = make([][]int64, n)
	for i := range matrix {
		matrix[i] = make([]int64, n)
	}

	// Fill defaults from NUMA affinity.
	for i := 0; i < n; i++ {
		di, _ := c.DeviceAt(order[i])
		for j := i + 1; j < n; j++ {
			dj, _ := c.DeviceAt(order[j])
			cost := c.DefaultCrossNuma
			if di.NumaNode == dj.NumaNode {
				cost = c.DefaultSameNuma
			}
			matrix[i][j] = cost
			matrix[j][i] = cost
		}
	}

	// Overlay explicit links and check for conflicts.
	type pair struct{ a, b int }
	declared := make(map[pair]bool)
	for _, l := range c.Links {
		if l.Cost < 0 {
			return nil, nil, nil, fmt.Errorf("link %q<->%q has negative cost", l.A, l.B)
		}
		ia, okA := index[l.A]
		ib, okB := index[l.B]
		if !okA || !okB {
			return nil, nil, nil, fmt.Errorf("link %q<->%q references an unknown device", l.A, l.B)
		}
		if ia == ib {
			return nil, nil, nil, fmt.Errorf("link %q<->%q is a self loop", l.A, l.B)
		}
		p := pair{ia, ib}
		if ia > ib {
			p = pair{ib, ia}
		}
		if declared[p] && matrix[ia][ib] != l.Cost {
			return nil, nil, nil, fmt.Errorf("conflicting links for %q<->%q: costs %d and %d", l.A, l.B, matrix[ia][ib], l.Cost)
		}
		declared[p] = true
		matrix[ia][ib] = l.Cost
		matrix[ib][ia] = l.Cost
	}

	return order, index, matrix, nil
}

func deviceIDs(ds []Device) []string {
	ids := make([]string, len(ds))
	for i, d := range ds {
		ids[i] = d.ID
	}
	return ids
}
