// Package builder 在 scanner 与 resolver 之上执行从编译目标（translation
// unit）出发的可达性遍历，构建稳定的依赖图，并负责：
//
//   - 循环引用检测（栈上回边规范化去重，不会无限递归）；
//   - 无法解析 include 的缺失节点与告警；
//   - 通过 Store 复用未变更文件的解析结果（增量扫描）。
package builder

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"depscanner/internal/resolver"
	"depscanner/internal/scanner"
)

// Options 为一次图构建的输入。
type Options struct {
	// Root 为项目根目录（绝对路径）。
	Root string
	// Targets 为入口编译目标，相对 Root 的正斜杠路径。
	Targets []string
	// QuoteDirs / SystemDirs 为搜索目录，相对路径按 Root 解释。
	QuoteDirs  []string
	SystemDirs []string
}

// StoredFile 是缓存中单个文件的解析记录（增量扫描时可直接复用）。
type StoredFile struct {
	Path    string    `json:"path"` // 图中使用的稳定显示路径（键）
	Abs     string    `json:"abs"`
	Size    int64     `json:"size"`
	ModTime time.Time `json:"mod_time"`
	// MTimeTrusted 表示 ModTime 与当前进程来自同一次 stat，单调时钟读数可信。
	// 从磁盘快照恢复的记录该字段为 false：JSON 往返会丢失单调时钟，
	// 此时不能仅凭 mtime 判等（粗粒度文件系统可能返回相同墙上时间），
	// 必须走一次内容 hash 校验。
	MTimeTrusted bool                 `json:"-"`
	Hash         string               `json:"hash"`
	Includes     []scanner.Include    `json:"includes"`
	Diagnostics  []scanner.Diagnostic `json:"diagnostics"`
}

// Store 为文件解析结果缓存。nil 表示不使用缓存（全量扫描）。
type Store interface {
	Get(displayPath string) (StoredFile, bool)
	Put(f StoredFile)
}

// Node 为依赖图中的文件节点。
type Node struct {
	Path    string `json:"path"`
	Kind    string `json:"kind"` // target | internal | external | missing
	Missing bool   `json:"missing,omitempty"`
	Size    int64  `json:"size,omitempty"`
	ModTime string `json:"mod_time,omitempty"` // RFC3339Nano UTC
	Hash    string `json:"hash,omitempty"`
}

// Edge 为依赖图中的有向边：From 依赖 To。
type Edge struct {
	From  string `json:"from"`
	To    string `json:"to"`
	Kind  string `json:"kind"`  // quoted | angled
	Scope string `json:"scope"` // relative | quote | system | absolute
	Line  int    `json:"line"`
}

// Stats 为构建统计。
type Stats struct {
	FilesTotal    int `json:"files_total"`
	FilesRead     int `json:"files_read"`     // 实际执行词法解析的文件数
	HashesChecked int `json:"hashes_checked"` // 仅做内容 hash 校验的文件数
	CacheHits     int `json:"cache_hits"`     // 完全复用缓存（未读内容）的文件数
	MissingFiles  int `json:"missing_files"`
	EdgesTotal    int `json:"edges_total"`
	CyclesTotal   int `json:"cycles_total"`
	WarningsTotal int `json:"warnings_total"`
	ErrorsTotal   int `json:"errors_total"`
}

// Result 为图构建结果。
type Result struct {
	Root        string               `json:"root"`
	Targets     []string             `json:"targets"`
	Nodes       []Node               `json:"nodes"`
	Edges       []Edge               `json:"edges"`
	Cycles      [][]string           `json:"cycles"`
	Diagnostics []scanner.Diagnostic `json:"diagnostics"`
	Stats       Stats                `json:"stats"`
	// Files 为已存在文件的解析记录，键为显示路径；供持久化快照使用。
	Files map[string]StoredFile `json:"-"`
}

type fileRec struct {
	info     StoredFile
	missing  bool
	includes []scanner.Include
}

type bctx struct {
	opts     Options
	resolver *resolver.Resolver
	store    Store

	files   map[string]*fileRec // 显示路径 -> 记录
	nodes   map[string]struct{}
	missing map[string]struct{}
	targets map[string]struct{}

	edges      map[[2]string]Edge
	diagSeen   map[string]struct{}
	diags      []scanner.Diagnostic
	cyclesSeen map[string]struct{}
	cycles     [][]string

	active  []string
	onStack map[string]int

	reads, hits, hashChecks int
}

// ValidateTarget 校验目标路径：非空、相对、不逃逸 root。
func ValidateTarget(_ string, target string) (string, error) {
	if target == "" {
		return "", fmt.Errorf("empty target")
	}
	if filepath.IsAbs(filepath.FromSlash(target)) {
		return "", fmt.Errorf("target must be relative to root: %q", target)
	}
	clean := filepath.ToSlash(filepath.Clean(filepath.FromSlash(target)))
	if clean == ".." || strings.HasPrefix(clean, "../") {
		return "", fmt.Errorf("target escapes root: %q", target)
	}
	return clean, nil
}

// Build 执行图构建。
func Build(opts Options, store Store) (*Result, error) {
	rootAbs, err := filepath.Abs(opts.Root)
	if err != nil {
		return nil, err
	}
	fi, err := os.Stat(rootAbs)
	if err != nil {
		return nil, fmt.Errorf("root: %w", err)
	}
	if !fi.IsDir() {
		return nil, fmt.Errorf("root is not a directory: %s", rootAbs)
	}
	if len(opts.Targets) == 0 {
		return nil, fmt.Errorf("no targets given")
	}

	r, err := resolver.New(rootAbs, opts.QuoteDirs, opts.SystemDirs)
	if err != nil {
		return nil, err
	}

	bc := &bctx{
		opts:       Options{Root: rootAbs, Targets: opts.Targets, QuoteDirs: opts.QuoteDirs, SystemDirs: opts.SystemDirs},
		resolver:   r,
		store:      store,
		files:      map[string]*fileRec{},
		nodes:      map[string]struct{}{},
		missing:    map[string]struct{}{},
		targets:    map[string]struct{}{},
		edges:      map[[2]string]Edge{},
		diagSeen:   map[string]struct{}{},
		cyclesSeen: map[string]struct{}{},
		onStack:    map[string]int{},
	}

	var cleanTargets []string
	for _, t := range opts.Targets {
		clean, verr := ValidateTarget(rootAbs, t)
		if verr != nil {
			return nil, verr
		}
		abs := filepath.Join(rootAbs, filepath.FromSlash(clean))
		sfi, serr := os.Stat(abs)
		if serr != nil || sfi.IsDir() {
			return nil, fmt.Errorf("target not found: %q", t)
		}
		cleanTargets = append(cleanTargets, clean)
		bc.targets[clean] = struct{}{}
	}

	for _, t := range cleanTargets {
		abs := filepath.Join(rootAbs, filepath.FromSlash(t))
		bc.visit(abs, t)
	}

	return bc.finalize(cleanTargets), nil
}

func (bc *bctx) display(abs string) string {
	abs = filepath.Clean(abs)
	if resolver.IsUnder(bc.opts.Root, abs) {
		rel, err := filepath.Rel(bc.opts.Root, abs)
		if err == nil {
			return filepath.ToSlash(rel)
		}
	}
	return abs
}

func (bc *bctx) addDiag(d scanner.Diagnostic) {
	key := string(d.Severity) + "|" + d.File + "|" + fmt.Sprint(d.Line) + "|" + d.Message
	if _, dup := bc.diagSeen[key]; dup {
		return
	}
	bc.diagSeen[key] = struct{}{}
	bc.diags = append(bc.diags, d)
}

func (bc *bctx) visit(abs, display string) {
	bc.nodes[display] = struct{}{}

	// 栈上回边：记录循环，不重复递归（必须先于已完成集合判断）。
	if _, on := bc.onStack[display]; on {
		bc.recordCycle(bc.active[bc.onStack[display]:], display)
		return
	}

	// 已完整扫描：直接返回。
	if _, done := bc.files[display]; done {
		return
	}

	rec, isMissing, err := bc.load(abs, display)
	if err != nil {
		bc.addDiag(scanner.Diagnostic{
			Severity: scanner.SeverityError,
			File:     display,
			Message:  fmt.Sprintf("read file: %v", err),
		})
		return
	}
	if isMissing {
		bc.missing[display] = struct{}{}
		return
	}

	bc.files[display] = &fileRec{info: rec, includes: rec.Includes}
	bc.onStack[display] = len(bc.active)
	bc.active = append(bc.active, display)

	for _, inc := range rec.Includes {
		res, ok := bc.resolver.Resolve(rec.Abs, inc)
		var toDisplay string
		var edge Edge
		if ok {
			toDisplay = bc.display(res.Abs)
			edge = Edge{From: display, To: toDisplay, Kind: string(inc.Kind), Scope: res.Scope, Line: inc.Line}
		} else {
			cand := bc.resolver.FirstCandidate(rec.Abs, inc)
			toDisplay = bc.display(cand)
			edge = Edge{From: display, To: toDisplay, Kind: string(inc.Kind), Scope: "", Line: inc.Line}
			bc.missing[toDisplay] = struct{}{}
			bc.nodes[toDisplay] = struct{}{}
			bc.addDiag(scanner.Diagnostic{
				Severity: scanner.SeverityWarning,
				File:     display,
				Line:     inc.Line,
				Message:  fmt.Sprintf("unresolved %s include %q", inc.Kind, inc.Path),
			})
		}
		key := [2]string{display, toDisplay}
		if _, exists := bc.edges[key]; !exists {
			bc.edges[key] = edge
		}
		if ok {
			bc.visit(res.Abs, toDisplay)
		}
	}

	bc.active = bc.active[:len(bc.active)-1]
	delete(bc.onStack, display)
}

// load 读取并扫描单个文件，命中缓存时复用结果。
func (bc *bctx) load(abs, display string) (StoredFile, bool, error) {
	sfi, err := os.Stat(abs)
	if err != nil {
		if os.IsNotExist(err) {
			return StoredFile{}, true, nil
		}
		return StoredFile{}, false, err
	}
	if sfi.IsDir() {
		return StoredFile{}, false, fmt.Errorf("%s is a directory", abs)
	}
	size := sfi.Size()
	mt := sfi.ModTime()

	cached, hasCache := bc.lookup(display)

	// 快路径：size 与 mtime 均一致才允许完全跳过读盘。
	// 从磁盘快照恢复的记录没有单调时钟，不允许走快路径。
	if hasCache && cached.MTimeTrusted && cached.Size == size && cached.ModTime.Equal(mt) {
		bc.hits++
		for _, d := range cached.Diagnostics {
			bc.addDiag(d)
		}
		return cached, false, nil
	}

	// 慢路径：读取内容计算 hash。某些文件系统 mtime 粒度较粗，
	// 快速重写会得到相同 mtime+size，因此存在缓存时必须再用 hash 兜底。
	data, err := os.ReadFile(abs)
	if err != nil {
		return StoredFile{}, false, err
	}
	sum := sha256.Sum256(data)
	hashStr := hex.EncodeToString(sum[:])
	if hasCache && cached.Hash == hashStr && cached.Size == size {
		bc.hashChecks++
		// 内容确实未变：复用解析结果，但同步刷新 mtime，避免每次都重算 hash。
		cached.ModTime = mt
		if bc.store != nil {
			bc.store.Put(cached)
		}
		for _, d := range cached.Diagnostics {
			bc.addDiag(d)
		}
		return cached, false, nil
	}

	includes, sdiags := scanner.Extract(display, data)
	for _, d := range sdiags {
		d.File = display
		bc.addDiag(d)
	}
	bc.reads++

	rec := StoredFile{
		Path:         display,
		Abs:          filepath.Clean(abs),
		Size:         size,
		ModTime:      mt,
		MTimeTrusted: true,
		Hash:         hashStr,
		Includes:     includes,
		Diagnostics:  append([]scanner.Diagnostic(nil), sdiags...),
	}
	if bc.store != nil {
		bc.store.Put(rec)
	}
	return rec, false, nil
}

func (bc *bctx) lookup(display string) (StoredFile, bool) {
	if bc.store == nil {
		return StoredFile{}, false
	}
	return bc.store.Get(display)
}

// recordCycle 将回边形成的环规范化后去重记录。
func (bc *bctx) recordCycle(stack []string, back string) {
	cyc := append(append([]string(nil), stack...), back)
	// cyc 形如 [a, b, c, a]；去掉末尾重复节点再旋转到最小成员开头。
	loop := cyc[:len(cyc)-1]
	minIdx := 0
	for i := 1; i < len(loop); i++ {
		if loop[i] < loop[minIdx] {
			minIdx = i
		}
	}
	rot := make([]string, 0, len(loop)+1)
	rot = append(rot, loop[minIdx:]...)
	rot = append(rot, loop[:minIdx]...)
	key := strings.Join(rot, " -> ")
	if _, dup := bc.cyclesSeen[key]; dup {
		return
	}
	bc.cyclesSeen[key] = struct{}{}
	rot = append(rot, rot[0]) // 闭合回起点，便于阅读
	bc.cycles = append(bc.cycles, rot)
}

func contains(m map[string]struct{}, k string) bool {
	_, ok := m[k]
	return ok
}

func (bc *bctx) finalize(targets []string) *Result {
	res := &Result{
		Root:        bc.opts.Root,
		Targets:     append([]string{}, targets...),
		Nodes:       []Node{},
		Edges:       []Edge{},
		Cycles:      [][]string{},
		Diagnostics: []scanner.Diagnostic{},
		Files:       map[string]StoredFile{},
	}
	if len(bc.diags) > 0 {
		res.Diagnostics = append(res.Diagnostics, bc.diags...)
	}

	for p := range bc.nodes {
		var kind string
		if _, isTarget := bc.targets[p]; isTarget {
			kind = "target"
		} else if _, isMissing := bc.missing[p]; isMissing {
			kind = "missing"
		} else if resolver.IsUnder(bc.opts.Root, bc.files[p].info.Abs) {
			kind = "internal"
		} else {
			kind = "external"
		}

		node := Node{Path: p, Kind: kind}
		if rec, ok := bc.files[p]; ok {
			node.Size = rec.info.Size
			node.ModTime = rec.info.ModTime.UTC().Format(time.RFC3339Nano)
			node.Hash = rec.info.Hash
			res.Files[p] = rec.info
		} else {
			node.Missing = true
		}
		res.Nodes = append(res.Nodes, node)
	}
	sort.Slice(res.Nodes, func(i, j int) bool { return res.Nodes[i].Path < res.Nodes[j].Path })

	for _, e := range bc.edges {
		res.Edges = append(res.Edges, e)
	}
	sort.Slice(res.Edges, func(i, j int) bool {
		if res.Edges[i].From != res.Edges[j].From {
			return res.Edges[i].From < res.Edges[j].From
		}
		if res.Edges[i].Line != res.Edges[j].Line {
			return res.Edges[i].Line < res.Edges[j].Line
		}
		return res.Edges[i].To < res.Edges[j].To
	})

	sort.Slice(bc.cycles, func(i, j int) bool {
		return strings.Join(bc.cycles[i], "|") < strings.Join(bc.cycles[j], "|")
	})
	res.Cycles = bc.cycles

	for _, d := range res.Diagnostics {
		switch d.Severity {
		case scanner.SeverityError:
			res.Stats.ErrorsTotal++
		case scanner.SeverityWarning:
			res.Stats.WarningsTotal++
		}
	}
	res.Stats.FilesTotal = len(res.Files)
	res.Stats.FilesRead = bc.reads
	res.Stats.HashesChecked = bc.hashChecks
	res.Stats.CacheHits = bc.hits
	res.Stats.MissingFiles = len(bc.missing)
	res.Stats.EdgesTotal = len(res.Edges)
	res.Stats.CyclesTotal = len(res.Cycles)

	return res
}
