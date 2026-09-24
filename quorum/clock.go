// Package quorum 实现一个 N 副本、可配置 W/R 的读写仲裁（quorum）模拟器。
//
// 该包只负责模拟本身：每个“副本”是一个内存中的版本集合，版本携带向量钟
// （vector clock）。副本之间的“网络”通过带延迟与截止时间（deadline）的
// 通道模拟：副本宕机即不响应（超时），写成功在 W 个副本确认后即返回，
// 读在 R 个副本响应后合并出最大版本集并对所有可达副本做读修复。
//
// 重要语义声明：本模拟器不使用全局线性化时间序。返回的兄弟版本
// （siblings）在向量钟下不可比较，即代表真正并发的历史；程序不会把
// 这种历史标记为线性一致（linearizable）。W+R>N 只保证读写法定人数
// 有交集，使“读集合”中至少有一个参与过最近一次已完成的写；它不解决
// 并发写冲突，也不保证历史的线性一致（见 README “语义边界”一节）。
package quorum

import (
	"fmt"
	"sort"
	"strings"
)

// Clock 是一个向量钟：节点逻辑ID -> 该节点计数器。
// 缺失的键按 0 处理。
type Clock map[string]int

// ClockLess 报告 a 是否在因果上严格先于 b（a -> b 且 a != b）。
// 比较在键的并集上逐分量进行，缺失分量按 0 计。
func ClockLess(a, b Clock) bool {
	lessSeen := false
	// 任一键上 a > b 都直接排除严格小于。
	for k, av := range a {
		bv := b[k]
		if av > bv {
			return false
		}
		if av < bv {
			lessSeen = true
		}
	}
	for k, bv := range b {
		if _, ok := a[k]; ok {
			continue
		}
		if 0 < bv {
			lessSeen = true
		}
	}
	return lessSeen
}

// ClockEqual 报告两个向量钟是否在所有分量上相等。
func ClockEqual(a, b Clock) bool {
	for k, av := range a {
		if av != 0 && b[k] != av {
			return false
		}
	}
	for k, bv := range b {
		if bv != 0 && a[k] != bv {
			return false
		}
	}
	return true
}

// ClockConcurrent 报告两个钟是否不可比较（既不严格先于、也不相等），
// 即对应历史真正并发。
func ClockConcurrent(a, b Clock) bool {
	return !ClockLess(a, b) && !ClockLess(b, a) && !ClockEqual(a, b)
}

// MergeClock 返回 a 与 b 的逐分量最大值合并结果（新写的父钟/反熵修复用）。
func MergeClock(a, b Clock) Clock {
	out := Clock{}
	for k, v := range a {
		out[k] = v
	}
	for k, v := range b {
		if v > out[k] {
			out[k] = v
		}
	}
	return out
}

// MergeClocks 返回多个钟的逐分量最大值合并结果。
func MergeClones(clocks ...Clock) Clock {
	out := Clock{}
	for _, c := range clocks {
		out = MergeClock(out, c)
	}
	return out
}

// Clone 返回钟的深拷贝，nil 安全。
func (c Clock) Clone() Clock {
	out := Clock{}
	for k, v := range c {
		out[k] = v
	}
	return out
}

// String 以稳定的 {a:1,b:2} 形式渲染钟，便于断言与展示。
func (c Clock) String() string {
	keys := make([]string, 0, len(c))
	for k, v := range c {
		if v == 0 {
			continue
		}
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, len(keys))
	for i, k := range keys {
		parts[i] = fmt.Sprintf("%s:%d", k, c[k])
	}
	return "{" + strings.Join(parts, ",") + "}"
}
