// Package engine 实现内容驱动的 DAG 增量构建引擎。
//
// 缓存键（SHA-256，规范 JSON）覆盖：
//   - 节点名与命令行（工具命令 + 渲染后参数）
//   - 工具版本 tool_version
//   - 参数 params（经模板渲染后的最终参数）
//   - 生效环境变量（仅工具声明继承的白名单 + 固定 env）
//   - 输入文件/目录的内容哈希（不含时间戳）
//   - 所有依赖节点的缓存键（传递依赖因此自动纳入）
package engine

import (
	"fmt"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"cdbg/internal/cache"
	"cdbg/internal/digest"
	"cdbg/internal/graph"
	"cdbg/internal/runner"
)

// 节点执行状态。
const (
	StatusBuilt   = "built"   // 实际执行了命令并发布缓存
	StatusCached  = "cached"  // 命中缓存，输出已恢复
	StatusFailed  = "failed"  // 命令失败/超时/输出缺失
	StatusSkipped = "skipped" // 上游失败而跳过
	StatusError   = "error"   // 构建前错误（如输入缺失）
)

// InputDigest 是单个声明输入的内容摘要。
type InputDigest struct {
	Path    string             `json:"path"`
	Kind    string             `json:"kind"` // file | dir
	Hash    string             `json:"hash"`
	Entries []digest.TreeEntry `json:"entries,omitempty"`
}

// Factors 是缓存键的全部组成。
type Factors struct {
	NodeName    string            `json:"node_name"`
	ToolName    string            `json:"tool_name"`
	ToolVersion string            `json:"tool_version"`
	Command     []string          `json:"command"`
	Shell       bool              `json:"shell"`
	Args        []string          `json:"args"`
	Params      map[string]string `json:"params"`
	Env         map[string]string `json:"env"`
	Inputs      []InputDigest     `json:"inputs"`
	DepKeys     map[string]string `json:"dep_keys"`
}

// NodeState 是工作目录维度持久化的节点状态。
type NodeState struct {
	Key      string    `json:"key"`
	Status   string    `json:"status"`
	Factors  *Factors  `json:"factors,omitempty"`
	RanAt    time.Time `json:"ran_at"`
	ExitCode int       `json:"exit_code,omitempty"`
	Error    string    `json:"error,omitempty"`
}

// NodeResult 是单次构建中一个节点的结果与解释。
type NodeResult struct {
	Name      string    `json:"name"`
	Status    string    `json:"status"`
	CacheKey  string    `json:"cache_key,omitempty"`
	Reasons   []string  `json:"reasons"` // 命中或失效的人类可读解释
	Changed   []string  `json:"changed,omitempty"`
	Outputs   []string  `json:"outputs,omitempty"`
	ExitCode  int       `json:"exit_code,omitempty"`
	Stdout    string    `json:"stdout,omitempty"`
	Stderr    string    `json:"stderr,omitempty"`
	TimedOut  bool      `json:"timed_out,omitempty"`
	Duration  string    `json:"duration,omitempty"`
	StartedAt time.Time `json:"started_at,omitempty"`
}

// Options 控制一次构建。
type Options struct {
	Spec    *graph.Spec
	Targets []string
	Force   bool // 忽略缓存查找（仍会发布）
	DryRun  bool // 只规划，不执行、不写缓存
}

// Engine 持有缓存、状态存储与命令执行器。
type Engine struct {
	cacheDir string
	store    *cache.Store
	states   *stateStore
	runner   *runner.Runner
	// inheritedEnv 是从父进程可继承的环境变量值映射。
	inheritedEnv map[string]string
	now          func() time.Time
}

// New 创建引擎。cacheDir 与 stateDir 都必须位于工作目录之外。
func New(cacheDir, stateDir string, inheritedEnv map[string]string) (*Engine, error) {
	store, err := cache.New(cacheDir)
	if err != nil {
		return nil, err
	}
	ss, err := newStateStore(stateDir)
	if err != nil {
		return nil, err
	}
	return &Engine{
		cacheDir:     cacheDir,
		store:        store,
		states:       ss,
		runner:       runner.New(),
		inheritedEnv: inheritedEnv,
		now:          time.Now,
	}, nil
}

// CacheEntries 列出缓存仓库中的全部条目元数据。
func (e *Engine) CacheEntries() ([]cache.EntryMeta, error) {
	return e.store.List()
}

// CacheEntry 返回指定键的条目元数据；不存在时返回 nil。
func (e *Engine) CacheEntry(key string) (*cache.EntryMeta, error) {
	ok, meta, err := e.store.Has(key)
	if err != nil || !ok {
		return nil, err
	}
	return meta, nil
}

// BuildResult 是一次构建的整体结果。
type BuildResult struct {
	Success  bool          `json:"success"`
	DryRun   bool          `json:"dry_run"`
	WorkDir  string        `json:"work_dir"`
	CacheDir string        `json:"cache_dir"`
	Order    []string      `json:"order"`
	Nodes    []*NodeResult `json:"nodes"`
	Cycle    []string      `json:"cycle,omitempty"`
	Error    string        `json:"error,omitempty"`
}

// Build 在 workdir 上执行一次增量构建。
func (e *Engine) Build(workdir string, opts Options) (*BuildResult, error) {
	abs, err := filepath.Abs(workdir)
	if err != nil {
		return nil, err
	}
	res := &BuildResult{WorkDir: abs, CacheDir: e.cacheDir, DryRun: opts.DryRun, Success: true}

	if err := opts.Spec.Validate(); err != nil {
		res.Success = false
		res.Error = err.Error()
		return res, nil
	}
	included, err := opts.Spec.Reachable(opts.Targets)
	if err != nil {
		res.Success = false
		res.Error = err.Error()
		return res, nil
	}
	order, err := opts.Spec.TopoOrder(included)
	if err != nil {
		res.Success = false
		if cyc, ok := err.(*graph.CycleError); ok {
			res.Cycle = cyc.Cycle
			res.Error = cyc.Error()
		} else {
			res.Error = err.Error()
		}
		return res, nil
	}
	res.Order = order

	if err := e.states.Load(abs); err != nil {
		return nil, err
	}

	success := map[string]bool{}
	currentKeys := map[string]string{} // 本次构建各节点最终使用/产生的键
	for _, name := range order {
		node := opts.Spec.NodeByName(name)
		tool := opts.Spec.Tools[node.Tool]
		nr := &NodeResult{Name: name, StartedAt: e.now()}

		// 上游失败：跳过。
		blockedBy := []string{}
		for _, d := range node.Deps {
			if included[d] && !success[d] {
				blockedBy = append(blockedBy, d)
			}
		}
		if len(blockedBy) > 0 {
			sort.Strings(blockedBy)
			nr.Status = StatusSkipped
			nr.Reasons = []string{fmt.Sprintf("依赖节点失败，跳过：%s", strings.Join(blockedBy, ", "))}
			// 跳过不更新该节点的持久化状态：保留其上次成功/失败记录用于解释。
			res.Nodes = append(res.Nodes, nr)
			continue
		}

		factors, ferr := e.computeFactors(abs, opts.Spec, node, tool, currentKeys)
		if ferr != nil {
			nr.Status = StatusError
			nr.Reasons = []string{"计算缓存键失败：" + ferr.Error()}
			nr.ExitCode = -1
			res.Nodes = append(res.Nodes, nr)
			res.Success = false
			e.recordFailure(abs, name, nil, ferr.Error(), -1)
			continue
		}
		key, kerr := digest.Canonical(factors)
		if kerr != nil {
			return nil, kerr
		}

		prev := e.states.Get(abs, name)
		hit, _, _ := e.store.Has(key)
		nr.CacheKey = key
		// 上次在本工作目录失败：即使内容寻址键恰好与更早的成功条目相同，
		// 也必须重新执行——最近一次的用户可见结果是失败。
		prevFailed := prev != nil && prev.Status == "failed"
		forceMiss := (opts.Force || prevFailed) && !opts.DryRun

		switch {
		case opts.DryRun:
			if hit {
				nr.Status = StatusCached
				nr.Reasons = e.hitReasons(key, factors, prev)
			} else {
				nr.Status = StatusBuilt
				nr.Reasons = e.missReasons(key, factors, prev)
			}
			nr.Outputs = append([]string{}, node.Outputs...)
		case hit && !forceMiss:
			nr.Status = StatusCached
			nr.Reasons = e.hitReasons(key, factors, prev)
			if _, rerr := e.store.Restore(key, abs); rerr != nil {
				nr.Status = StatusError
				nr.Reasons = []string{"恢复缓存失败：" + rerr.Error()}
				res.Success = false
				res.Nodes = append(res.Nodes, nr)
				continue
			}
			nr.Outputs = append([]string{}, node.Outputs...)
			e.recordSuccess(abs, name, key, factors)
		default:
			nr.Status = StatusBuilt
			switch {
			case opts.Force && hit:
				nr.Reasons = append(nr.Reasons, "force=true：缓存条目存在但强制重新执行")
			case prevFailed:
				nr.Reasons = append(nr.Reasons, "本节点上次构建失败，重新执行（失败不发布缓存，也不直接复用同键旧条目）")
			default:
				nr.Reasons = e.missReasons(key, factors, prev)
			}
			runResult, rerr := e.runner.Run(abs, tool, node, e.inheritedEnv)
			if rerr != nil {
				nr.Status = StatusError
				nr.ExitCode = -1
				nr.Reasons = append(nr.Reasons, "无法启动命令："+rerr.Error())
				res.Success = false
				res.Nodes = append(res.Nodes, nr)
				e.recordFailure(abs, name, factors, rerr.Error(), -1)
				continue
			}
			nr.Duration = runResult.Duration
			nr.Stdout = runResult.Stdout
			nr.Stderr = runResult.Stderr
			nr.TimedOut = runResult.TimedOut
			nr.ExitCode = runResult.ExitCode

			if runResult.TimedOut {
				nr.Status = StatusFailed
				nr.Reasons = append(nr.Reasons, fmt.Sprintf("命令超时（timeout_sec=%d 生效）", effectiveTimeout(node)))
				res.Success = false
				res.Nodes = append(res.Nodes, nr)
				e.recordFailure(abs, name, factors, "command timed out", runResult.ExitCode)
				continue
			}
			if runResult.ExitCode != 0 {
				nr.Status = StatusFailed
				nr.Reasons = append(nr.Reasons, fmt.Sprintf("命令以非零退出码 %d 结束；失败节点不发布缓存", runResult.ExitCode))
				res.Success = false
				res.Nodes = append(res.Nodes, nr)
				e.recordFailure(abs, name, factors, fmt.Sprintf("exit code %d", runResult.ExitCode), runResult.ExitCode)
				continue
			}
			// 成功退出后仍须校验声明输出真实存在，否则视为失败且不发布。
			missing := missingOutputs(abs, node.Outputs)
			if len(missing) > 0 {
				msg := fmt.Sprintf("命令成功但声明输出缺失：%s；不发布缓存", strings.Join(missing, ", "))
				nr.Status = StatusFailed
				nr.ExitCode = 0
				nr.Reasons = append(nr.Reasons, msg)
				res.Success = false
				res.Nodes = append(res.Nodes, nr)
				e.recordFailure(abs, name, factors, msg, 0)
				continue
			}

			meta := &cache.EntryMeta{
				Key:       key,
				Node:      name,
				Outputs:   append([]string{}, node.Outputs...),
				CreatedAt: e.now(),
				KeyFactor: factorSummary(factors),
			}
			if perr := e.store.Publish(key, abs, node.Outputs, meta); perr != nil {
				nr.Status = StatusError
				nr.Reasons = append(nr.Reasons, "发布缓存失败："+perr.Error())
				res.Success = false
				res.Nodes = append(res.Nodes, nr)
				e.recordFailure(abs, name, factors, perr.Error(), 0)
				continue
			}
			nr.Outputs = append([]string{}, node.Outputs...)
			nr.Reasons = append(nr.Reasons, "构建成功，已发布缓存条目 "+key[:12]+"…")
			e.recordSuccess(abs, name, key, factors)
		}

		success[name] = true
		currentKeys[name] = key
		res.Nodes = append(res.Nodes, nr)
	}
	return res, nil
}

func effectiveTimeout(node *graph.Node) int {
	if node.TimeoutSec > 0 {
		return node.TimeoutSec
	}
	return 600
}

// computeFactors 收集节点缓存键的全部因子。
// currentKeys 提供本次构建中依赖节点刚产生的键（force/失败恢复场景）。
func (e *Engine) computeFactors(workdir string, spec *graph.Spec, node *graph.Node, tool *graph.Tool, currentKeys map[string]string) (*Factors, error) {
	rendered, err := runner.RenderArgs(tool, node)
	if err != nil {
		return nil, err
	}
	inputs := make([]InputDigest, 0, len(node.Inputs))
	for _, in := range node.Inputs {
		full := filepath.Join(workdir, filepath.FromSlash(in))
		info, err := statFile(full)
		if err != nil {
			return nil, fmt.Errorf("输入 %q 不可读: %w", in, err)
		}
		id := InputDigest{Path: in}
		if info.IsDir() {
			h, entries, err := digest.Tree(full)
			if err != nil {
				return nil, fmt.Errorf("输入目录 %q: %w", in, err)
			}
			id.Kind, id.Hash, id.Entries = "dir", h, entries
		} else {
			h, err := digest.File(full)
			if err != nil {
				return nil, fmt.Errorf("输入文件 %q: %w", in, err)
			}
			id.Kind, id.Hash = "file", h
		}
		inputs = append(inputs, id)
	}
	sort.Slice(inputs, func(i, j int) bool { return inputs[i].Path < inputs[j].Path })

	depKeys := map[string]string{}
	for _, d := range node.Deps {
		if k, ok := currentKeys[d]; ok && k != "" {
			// 本次构建中依赖刚执行/命中，优先用它本次的实际键。
			depKeys[d] = k
			continue
		}
		st := e.states.Get(workdir, d)
		if st == nil || st.Key == "" {
			// 理论上不会到达（依赖失败本节点会被跳过），保险起见给空键，
			// 使本节点键必然不同于任何成功历史键。
			depKeys[d] = ""
			continue
		}
		depKeys[d] = st.Key
	}

	env := map[string]string{}
	declared := append([]string{}, tool.DeclaredEnv...)
	sort.Strings(declared)
	for _, k := range declared {
		if v, ok := e.inheritedEnv[k]; ok {
			env[k] = v
		}
	}
	for k, v := range tool.Env {
		env[k] = v
	}

	params := map[string]string{}
	for k, v := range node.Params {
		params[k] = v
	}

	return &Factors{
		NodeName:    node.Name,
		ToolName:    tool.Name,
		ToolVersion: tool.ToolVersion,
		Command:     append([]string{}, tool.Command...),
		Shell:       tool.Shell,
		Args:        rendered,
		Params:      params,
		Env:         env,
		Inputs:      inputs,
		DepKeys:     depKeys,
	}, nil
}

func (e *Engine) recordSuccess(workdir, name, key string, f *Factors) {
	e.states.Put(workdir, name, NodeState{
		Key:     key,
		Status:  "succeeded",
		Factors: f,
		RanAt:   e.now(),
	})
}

func (e *Engine) recordFailure(workdir, name string, f *Factors, errMsg string, exitCode int) {
	// 失败状态不持有可用缓存键（失败不发布）；保留因子便于下次对比解释。
	e.states.Put(workdir, name, NodeState{
		Status:   "failed",
		Factors:  f,
		RanAt:    e.now(),
		ExitCode: exitCode,
		Error:    errMsg,
	})
}

func missingOutputs(workdir string, outputs []string) []string {
	var missing []string
	for _, out := range outputs {
		if _, err := statFile(filepath.Join(workdir, filepath.FromSlash(out))); err != nil {
			missing = append(missing, out)
		}
	}
	return missing
}

func factorSummary(f *Factors) *cache.KeyFactorSummary {
	return &cache.KeyFactorSummary{
		ToolName:    f.ToolName,
		ToolVersion: f.ToolVersion,
		Args:        f.Args,
		Params:      f.Params,
		Env:         f.Env,
		Inputs: func() []digest.TreeEntry {
			var es []digest.TreeEntry
			for _, in := range f.Inputs {
				es = append(es, in.Entries...)
			}
			return es
		}(),
		Deps: f.DepKeys,
	}
}
