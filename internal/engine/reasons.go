package engine

import (
	"fmt"
	"sort"
	"strings"
)

// hitReasons 解释为什么命中缓存。
func (e *Engine) hitReasons(key string, f *Factors, prev *NodeState) []string {
	r := []string{fmt.Sprintf("缓存命中：键 %s 存在且键内全部因子未变", key[:12]+"…")}
	if prev != nil && prev.Key == key {
		r = append(r, "与本工作目录上次成功构建的缓存键一致")
	}
	r = append(r, describeFactors(f)...)
	return r
}

// missReasons 通过与工作目录持久化的上次因子逐项对比，解释为何未命中。
func (e *Engine) missReasons(key string, f *Factors, prev *NodeState) []string {
	if prev == nil {
		return []string{"缓存失效：本工作目录中该节点从未成功构建过（首次构建）"}
	}
	if prev.Status == "failed" {
		r := []string{"缓存失效：上次构建失败，失败节点不会发布缓存"}
		if prev.Error != "" {
			r = append(r, "上次失败原因："+prev.Error)
		}
		r = append(r, diffFactors(prev.Factors, f)...)
		return r
	}

	r := []string{fmt.Sprintf("缓存失效：本次键 %s 与上次键 %s 不同", key[:12]+"…", short(prev.Key))}
	if exists, _, _ := e.store.Has(key); !exists {
		r = append(r, "新键在缓存仓库中不存在（对应条目可能已被清理）")
	}
	r = append(r, diffFactors(prev.Factors, f)...)
	return r
}

func short(k string) string {
	if len(k) > 12 {
		return k[:12] + "…"
	}
	return k
}

// diffFactors 返回旧因子 -> 新因子的具体差异列表。
func diffFactors(old, new *Factors) []string {
	var r []string
	if old == nil {
		return r
	}
	if old.ToolVersion != new.ToolVersion {
		r = append(r, fmt.Sprintf("工具版本变化：%s %q → %q", new.ToolName, old.ToolVersion, new.ToolVersion))
	}
	if !equalStrings(old.Command, new.Command) {
		r = append(r, fmt.Sprintf("工具命令变化：%v → %v", old.Command, new.Command))
	}
	if old.Shell != new.Shell {
		r = append(r, fmt.Sprintf("执行模式变化：shell=%v → shell=%v", old.Shell, new.Shell))
	}
	if !equalStrings(old.Args, new.Args) {
		r = append(r, fmt.Sprintf("渲染后参数变化：%v → %v", old.Args, new.Args))
	}
	if d := diffParams(old.Params, new.Params); d != "" {
		r = append(r, "参数变化："+d)
	}
	if d := diffParams(old.Env, new.Env); d != "" {
		r = append(r, "声明环境变量变化："+d)
	}
	if d := diffInputs(old.Inputs, new.Inputs); d != "" {
		r = append(r, "输入内容变化（仅按内容比较，与 mtime 无关）："+d)
	}
	if d := diffDeps(old.DepKeys, new.DepKeys); d != "" {
		r = append(r, "依赖缓存键变化（传递依赖变更沿 DAG 传播）："+d)
	}
	return r
}

func describeFactors(f *Factors) []string {
	return []string{
		fmt.Sprintf("因子：工具=%s@%s，参数=%s，声明环境变量=%s，输入=%d 项，依赖=%d 个",
			f.ToolName, f.ToolVersion, mapDesc(f.Params), mapDesc(f.Env), len(f.Inputs), len(f.DepKeys)),
	}
}

func mapDesc(m map[string]string) string {
	if len(m) == 0 {
		return "{}"
	}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+m[k])
	}
	return "{" + strings.Join(parts, ", ") + "}"
}

func diffParams(old, new map[string]string) string {
	keys := map[string]bool{}
	for k := range old {
		keys[k] = true
	}
	for k := range new {
		keys[k] = true
	}
	var changes []string
	for k := range keys {
		ov, ook := old[k]
		nv, nok := new[k]
		switch {
		case !ook:
			changes = append(changes, k+" 新增="+nv)
		case !nok:
			changes = append(changes, k+" 已删除(原="+ov+")")
		case ov != nv:
			changes = append(changes, k+" "+ov+" → "+nv)
		}
	}
	sort.Strings(changes)
	return strings.Join(changes, ", ")
}

func diffInputs(old, new []InputDigest) string {
	oldM := map[string]InputDigest{}
	newM := map[string]InputDigest{}
	for _, in := range old {
		oldM[in.Path] = in
	}
	for _, in := range new {
		newM[in.Path] = in
	}
	keys := map[string]bool{}
	for k := range oldM {
		keys[k] = true
	}
	for k := range newM {
		keys[k] = true
	}
	var changes []string
	for k := range keys {
		o, ook := oldM[k]
		n, nok := newM[k]
		switch {
		case !ook:
			changes = append(changes, k+" 新增("+n.Kind+")")
		case !nok:
			changes = append(changes, k+" 已删除")
		case o.Hash != n.Hash:
			changes = append(changes, k+" "+short(o.Hash)+" → "+short(n.Hash))
		}
	}
	sort.Strings(changes)
	return strings.Join(changes, ", ")
}

func diffDeps(old, new map[string]string) string {
	keys := map[string]bool{}
	for k := range old {
		keys[k] = true
	}
	for k := range new {
		keys[k] = true
	}
	var changes []string
	for k := range keys {
		ov := old[k]
		nv := new[k]
		switch {
		case ov == "":
			changes = append(changes, k+" 新增依赖→"+short(nv))
		case nv == "":
			changes = append(changes, k+" 依赖已移除")
		case ov != nv:
			changes = append(changes, k+" "+short(ov)+" → "+short(nv))
		}
	}
	sort.Strings(changes)
	return strings.Join(changes, ", ")
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
