package service

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"depscanner/internal/builder"
	"depscanner/internal/scanner"
)

// ScanRequest 为 POST /api/v1/scan 的请求体。
type ScanRequest struct {
	Root              string   `json:"root"`
	Targets           []string `json:"targets"`
	QuoteIncludeDirs  []string `json:"quote_include_dirs"`
	SystemIncludeDirs []string `json:"system_include_dirs"`
	CacheDir          string   `json:"cache_dir"`
}

// AffectedRequest 为 POST /api/v1/affected 的请求体。
type AffectedRequest struct {
	ScanRequest
	// UpdateBaseline 为 true 时，分析完成后用当前图更新基线。
	UpdateBaseline bool `json:"update_baseline"`
}

// ParseRequest 为 POST /api/v1/parse 的请求体（单文件词法解析）。
type ParseRequest struct {
	Root string `json:"root"`
	File string `json:"file"`
}

// Changed 汇总相对基线的文件变更。
type Changed struct {
	Added    []string `json:"added"`
	Modified []string `json:"modified"`
	Deleted  []string `json:"deleted"`
}

// ScanResponse 为扫描结果。
type ScanResponse struct {
	Status        string               `json:"status"` // ok | error
	Root          string               `json:"root"`
	Graph         *builder.Result      `json:"graph"`
	Diagnostics   []scanner.Diagnostic `json:"diagnostics"`
	Baseline      *BaselineInfo        `json:"baseline,omitempty"`
	ConfigChanged bool                 `json:"config_changed,omitempty"`
}

// AffectedResponse 为增量分析结果。
type AffectedResponse struct {
	Status          string               `json:"status"`
	Root            string               `json:"root"`
	Baseline        *BaselineInfo        `json:"baseline,omitempty"`
	ConfigChanged   bool                 `json:"config_changed"`
	Changed         Changed              `json:"changed"`
	AffectedFiles   []string             `json:"affected_files"`
	AffectedTargets []string             `json:"affected_targets"`
	Graph           *builder.Result      `json:"graph"`
	Diagnostics     []scanner.Diagnostic `json:"diagnostics"`
}

// BaselineInfo 描述本次分析使用的基线。
type BaselineInfo struct {
	Path      string    `json:"path"`
	Existed   bool      `json:"existed"`
	ScannedAt time.Time `json:"scanned_at"`
}

// ParseResponse 为单文件解析结果。
type ParseResponse struct {
	Status      string               `json:"status"`
	File        string               `json:"file"`
	Includes    []scanner.Include    `json:"includes"`
	Diagnostics []scanner.Diagnostic `json:"diagnostics"`
}

func hashHex(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

func normalizeDirs(rootAbs string, dirs []string) []string {
	var out []string
	for _, d := range dirs {
		d = strings.TrimSpace(d)
		if d == "" {
			continue
		}
		if !filepath.IsAbs(filepath.FromSlash(d)) {
			d = filepath.Join(rootAbs, filepath.FromSlash(d))
		}
		info, err := os.Stat(d)
		if err != nil || !info.IsDir() {
			// 不存在的搜索目录按 C 编译器习惯静默忽略；不参与候选。
			continue
		}
		abs, _ := filepath.Abs(d)
		out = append(out, abs)
	}
	return out
}

func validateCommon(root, cacheDir string, targets []string) (string, string, error) {
	rootAbs, err := filepath.Abs(strings.TrimSpace(root))
	if err != nil {
		return "", "", fmt.Errorf("root: %w", err)
	}
	fi, err := os.Stat(rootAbs)
	if err != nil {
		return "", "", fmt.Errorf("root: %w", err)
	}
	if !fi.IsDir() {
		return "", "", fmt.Errorf("root is not a directory: %s", rootAbs)
	}
	if len(targets) == 0 {
		return "", "", fmt.Errorf("targets must not be empty")
	}
	cacheAbs, err := filepath.Abs(strings.TrimSpace(cacheDir))
	if err != nil {
		return "", "", fmt.Errorf("cache_dir: %w", err)
	}
	if err := ensureCacheSeparation(rootAbs, cacheAbs); err != nil {
		return "", "", err
	}
	if err := os.MkdirAll(cacheAbs, 0o755); err != nil {
		return "", "", fmt.Errorf("create cache dir: %w", err)
	}
	return rootAbs, cacheAbs, nil
}

func statusFromDiags(diags []scanner.Diagnostic) string {
	for _, d := range diags {
		if d.Severity == scanner.SeverityError {
			return "error"
		}
	}
	return "ok"
}

// Engine 承载服务业务逻辑，与 HTTP 传输无关，方便 CLI 直接复用与测试。
type Engine struct{}

// Scan 执行一次（带缓存复用的）扫描并写入基线。
func (Engine) Scan(req ScanRequest) (*ScanResponse, error) {
	rootAbs, cacheAbs, err := validateCommon(req.Root, req.CacheDir, req.Targets)
	if err != nil {
		return nil, &RequestError{Msg: err.Error()}
	}

	sys := normalizeDirs(rootAbs, req.SystemIncludeDirs)
	quo := normalizeDirs(rootAbs, req.QuoteIncludeDirs)

	bp := snapshotPath(cacheAbs, rootAbs)
	prev, _ := loadSnapshot(bp) // 损坏/缺基线不阻断扫描
	store := newMemStore()
	configChanged := false
	if prev != nil {
		if !sameConfig(prev.Config, Config{Targets: req.Targets, QuoteDirs: quo, SystemDirs: sys}) {
			configChanged = true
		} else {
			for p, f := range prev.Files {
				store.files[p] = f
			}
		}
	}

	graph, err := builder.Build(builder.Options{
		Root:       rootAbs,
		Targets:    append([]string(nil), req.Targets...),
		QuoteDirs:  quo,
		SystemDirs: sys,
	}, store)
	if err != nil {
		return nil, &RequestError{Msg: err.Error()}
	}

	snap := &Snapshot{
		Version: snapshotVersion,
		Root:    rootAbs,
		Config:  Config{Targets: append([]string(nil), graph.Targets...), QuoteDirs: quo, SystemDirs: sys},
		Graph:   graph,
		Files:   graph.Files,
	}
	baselineExisted := prev != nil
	scanErr := saveSnapshot(bp, snap)
	if scanErr != nil {
		return nil, fmt.Errorf("save baseline: %w", scanErr)
	}

	return &ScanResponse{
		Status:        statusFromDiags(graph.Diagnostics),
		Root:          rootAbs,
		Graph:         graph,
		Diagnostics:   graph.Diagnostics,
		ConfigChanged: configChanged,
		Baseline: &BaselineInfo{
			Path:      bp,
			Existed:   baselineExisted,
			ScannedAt: time.Now().UTC(),
		},
	}, nil
}

// Affected 对比基线计算变更集与受影响目标。
func (Engine) Affected(req AffectedRequest) (*AffectedResponse, error) {
	rootAbs, cacheAbs, err := validateCommon(req.Root, req.CacheDir, req.Targets)
	if err != nil {
		return nil, &RequestError{Msg: err.Error()}
	}

	sys := normalizeDirs(rootAbs, req.SystemIncludeDirs)
	quo := normalizeDirs(rootAbs, req.QuoteIncludeDirs)
	cfg := Config{Targets: append([]string(nil), req.Targets...), QuoteDirs: quo, SystemDirs: sys}

	bp := snapshotPath(cacheAbs, rootAbs)
	prev, loadErr := loadSnapshot(bp)
	if loadErr != nil {
		return nil, fmt.Errorf("load baseline: %w", loadErr)
	}

	resp := &AffectedResponse{Root: rootAbs}
	if prev != nil {
		info := &BaselineInfo{Path: bp, Existed: true}
		resp.Baseline = info
	} else {
		resp.Baseline = &BaselineInfo{Path: bp, Existed: false}
	}

	store := newMemStore()
	configChanged := true
	if prev != nil && sameConfig(prev.Config, cfg) {
		configChanged = false
		for p, f := range prev.Files {
			store.files[p] = f
		}
	}
	resp.ConfigChanged = configChanged

	graph, err := builder.Build(builder.Options{
		Root:       rootAbs,
		Targets:    append([]string(nil), req.Targets...),
		QuoteDirs:  quo,
		SystemDirs: sys,
	}, store)
	if err != nil {
		return nil, &RequestError{Msg: err.Error()}
	}
	resp.Graph = graph
	resp.Diagnostics = graph.Diagnostics
	resp.Status = statusFromDiags(graph.Diagnostics)

	// 配置变化：全部目标受影响，不做细粒度差异。
	if configChanged {
		resp.Changed = Changed{Added: []string{}, Modified: []string{}, Deleted: []string{}}
		resp.AffectedFiles = append([]string{}, nodePaths(graph.Nodes)...)
		resp.AffectedTargets = append([]string{}, graph.Targets...)
	} else {
		changed := diffSnapshots(prev, graph)
		sort.Strings(changed.Added)
		sort.Strings(changed.Modified)
		sort.Strings(changed.Deleted)
		resp.Changed = changed

		changedSet := map[string]struct{}{}
		for _, p := range changed.Added {
			changedSet[p] = struct{}{}
		}
		for _, p := range changed.Modified {
			changedSet[p] = struct{}{}
		}
		for _, p := range changed.Deleted {
			changedSet[p] = struct{}{}
		}

		// 删除的文件已不在当前图中，需要在旧图上反向追溯；
		// 其余变更在当前图上反向追溯。取两者并集。
		reachCurrent := reverseClosure(graph.Edges, changedSet)
		deletedSet := map[string]struct{}{}
		for _, p := range changed.Deleted {
			deletedSet[p] = struct{}{}
		}
		reachOld := reverseClosure(prev.Graph.Edges, deletedSet)

		affected := map[string]struct{}{}
		for p := range reachCurrent {
			affected[p] = struct{}{}
		}
		for p := range reachOld {
			affected[p] = struct{}{}
		}
		resp.AffectedFiles = sortedKeys(affected)

		targetSet := map[string]struct{}{}
		for _, t := range graph.Targets {
			targetSet[t] = struct{}{}
		}
		affTargets := []string{}
		for p := range affected {
			if _, ok := targetSet[p]; ok {
				affTargets = append(affTargets, p)
			}
		}
		sort.Strings(affTargets)
		resp.AffectedTargets = affTargets
	}

	// 带 update_baseline 时把基线整体推进到当前状态：图与文件记录都更新。
	// 典型 CI 用法：先多次只读 affected 观察影响面，确认后再落一次基线；
	// 这样下次删除文件时，反向追溯所用的正是“上一次基线”的图边。
	if req.UpdateBaseline {
		snap := &Snapshot{
			Version: snapshotVersion,
			Root:    rootAbs,
			Config:  cfg,
			Graph:   graph,
			Files:   graph.Files,
		}
		if err := saveSnapshot(bp, snap); err != nil {
			return nil, fmt.Errorf("save baseline: %w", err)
		}
	}
	return resp, nil
}

// ParseFile 仅做单文件词法解析，不解析搜索路径。
func (Engine) ParseFile(req ParseRequest) (*ParseResponse, error) {
	rootAbs, err := filepath.Abs(strings.TrimSpace(req.Root))
	if err != nil {
		return nil, &RequestError{Msg: "root: " + err.Error()}
	}
	fi, err := os.Stat(rootAbs)
	if err != nil || !fi.IsDir() {
		return nil, &RequestError{Msg: "root is not a directory"}
	}
	if strings.TrimSpace(req.File) == "" {
		return nil, &RequestError{Msg: "file must not be empty"}
	}
	var abs string
	if filepath.IsAbs(filepath.FromSlash(req.File)) {
		abs = filepath.Clean(filepath.FromSlash(req.File))
	} else {
		clean, verr := builder.ValidateTarget(rootAbs, req.File)
		if verr != nil {
			return nil, &RequestError{Msg: verr.Error()}
		}
		abs = filepath.Join(rootAbs, filepath.FromSlash(clean))
	}
	data, err := os.ReadFile(abs)
	if err != nil {
		return nil, &RequestError{Msg: fmt.Sprintf("read file: %v", err)}
	}
	display := abs
	if rel, rerr := filepath.Rel(rootAbs, abs); rerr == nil && !strings.HasPrefix(rel, "..") {
		display = filepath.ToSlash(rel)
	}
	includes, diags := scanner.Extract(display, data)
	if includes == nil {
		includes = []scanner.Include{}
	}
	if diags == nil {
		diags = []scanner.Diagnostic{}
	}
	return &ParseResponse{
		Status:      statusFromDiags(diags),
		File:        display,
		Includes:    includes,
		Diagnostics: diags,
	}, nil
}

// diffSnapshots 对比基线与当前图的文件记录。
func diffSnapshots(prev *Snapshot, cur *builder.Result) Changed {
	ch := Changed{Added: []string{}, Modified: []string{}, Deleted: []string{}}
	for p, curF := range cur.Files {
		oldF, existed := prev.Files[p]
		if !existed {
			ch.Added = append(ch.Added, p)
		} else if oldF.Hash != curF.Hash || oldF.Size != curF.Size ||
			!includesEqual(oldF.Includes, curF.Includes) {
			ch.Modified = append(ch.Modified, p)
		}
	}
	for p := range prev.Files {
		if _, exists := cur.Files[p]; !exists {
			ch.Deleted = append(ch.Deleted, p)
		}
	}
	// 基线中缺失（未解析）、当前仍缺失的节点不在 Files 内，无需比对。
	// 但“之前缺失、现在被找到”的文件会在当前 Files 中表现为新增。
	return ch
}

func includesEqual(a, b []scanner.Include) bool {
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

func sameConfig(a, b Config) bool {
	return stringSliceEqual(a.Targets, b.Targets) &&
		stringSliceEqual(a.QuoteDirs, b.QuoteDirs) &&
		stringSliceEqual(a.SystemDirs, b.SystemDirs)
}

func stringSliceEqual(a, b []string) bool {
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

func nodePaths(nodes []builder.Node) []string {
	out := make([]string, 0, len(nodes))
	for _, n := range nodes {
		out = append(out, n.Path)
	}
	sort.Strings(out)
	return out
}

// RequestError 表示由请求本身导致的可预期错误（对应 HTTP 400）。
type RequestError struct{ Msg string }

func (e *RequestError) Error() string { return e.Msg }
