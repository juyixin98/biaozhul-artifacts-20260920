package engine

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"cdbg/internal/graph"
)

// 构造一个最小但包含“生成 -> 合并 -> 哈希”链 + 一个无关节点的图。
func testSpec(t *testing.T) *graph.Spec {
	t.Helper()
	return &graph.Spec{
		Version: "1",
		Tools: map[string]*graph.Tool{
			"write": {
				Name: "write", ToolVersion: "1.0.0", Shell: true,
				Command: []string{"mkdir -p \"$(dirname \"$1\")\" && printf '%s' \"$2\" > \"$1\""},
			},
			"concat": {
				Name: "concat", ToolVersion: "1.0.0", Shell: true,
				Command: []string{"cat \"$1\" \"$2\" > \"$3\""},
			},
			"sha": {
				Name: "sha", ToolVersion: "1.0.0", Shell: true,
				Command: []string{"sha256sum \"$1\" > \"$2\""},
			},
			"fail": {
				Name: "fail", ToolVersion: "1.0.0", Shell: true,
				Command: []string{"echo boom >&2; exit 7"},
			},
		},
		Nodes: []*graph.Node{
			{Name: "gen_src", Tool: "write", Outputs: []string{"src.txt"},
				Params: map[string]string{"c": "SRC1"}, Args: []string{"src.txt", "{{.c}}"}},
			{Name: "gen_hdr", Tool: "write", Outputs: []string{"hdr.txt"},
				Params: map[string]string{"c": "HDR1"}, Args: []string{"hdr.txt", "{{.c}}"}},
			{Name: "merge", Tool: "concat", Deps: []string{"gen_src", "gen_hdr"},
				Outputs: []string{"merged.txt"}, Args: []string{"hdr.txt", "src.txt", "merged.txt"}},
			{Name: "hash", Tool: "sha", Deps: []string{"merge"},
				Outputs: []string{"merged.sha"}, Args: []string{"merged.txt", "merged.sha"}},
			{Name: "solo", Tool: "write", Outputs: []string{"solo.txt"},
				Params: map[string]string{"c": "SOLO"}, Args: []string{"solo.txt", "{{.c}}"}},
		},
	}
}

func newTestEngine(t *testing.T) (*Engine, string) {
	t.Helper()
	base := t.TempDir()
	work := filepath.Join(base, "work")
	cache := filepath.Join(base, "cache")
	state := filepath.Join(base, "state")
	if err := os.MkdirAll(work, 0o755); err != nil {
		t.Fatal(err)
	}
	eng, err := New(cache, state, map[string]string{})
	if err != nil {
		t.Fatal(err)
	}
	return eng, work
}

func statusMap(res *BuildResult) map[string]*NodeResult {
	m := map[string]*NodeResult{}
	for _, n := range res.Nodes {
		m[n.Name] = n
	}
	return m
}

func TestFullBuildThenAllCached(t *testing.T) {
	eng, work := newTestEngine(t)
	spec := testSpec(t)

	res, err := eng.Build(work, Options{Spec: spec})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Success {
		t.Fatalf("first build failed: %s", res.Error)
	}
	for _, n := range res.Nodes {
		if n.Status != StatusBuilt {
			t.Fatalf("first build: node %s status %s, want built", n.Name, n.Status)
		}
	}

	// 第二次构建：全部缓存命中。
	res2, err := eng.Build(work, Options{Spec: spec})
	if err != nil {
		t.Fatal(err)
	}
	if !res2.Success {
		t.Fatalf("second build failed: %s", res2.Error)
	}
	for _, n := range res2.Nodes {
		if n.Status != StatusCached {
			t.Fatalf("second build: node %s status %s, want cached (reasons=%v)", n.Name, n.Status, n.Reasons)
		}
	}
}

func TestMtimeOnlyChangeIsFullCacheHit(t *testing.T) {
	eng, work := newTestEngine(t)
	spec := testSpec(t)
	if _, err := eng.Build(work, Options{Spec: spec}); !errIsNil(t, err) {
		return
	}
	// 把工作目录内文件的 mtime 改成很久以前，内容不动。
	old := time.Now().Add(-48 * time.Hour)
	_ = filepath.Walk(work, func(path string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			_ = os.Chtimes(path, old, old)
		}
		return nil
	})
	res, err := eng.Build(work, Options{Spec: spec})
	if err != nil {
		t.Fatal(err)
	}
	for _, n := range res.Nodes {
		if n.Status != StatusCached {
			t.Fatalf("mtime-only change rebuilt %s: %v", n.Name, n.Reasons)
		}
	}
}

func TestContentChangeRebuildsTransitiveChainOnly(t *testing.T) {
	eng, work := newTestEngine(t)
	spec := testSpec(t)
	if r, _ := eng.Build(work, Options{Spec: spec}); !r.Success {
		t.Fatal("first build failed")
	}

	// 修改传递依赖：gen_src 的参数（即文件内容），保持 mtime 为旧值。
	srcPath := filepath.Join(work, "src.txt")
	info, _ := os.Stat(srcPath)
	oldMtime := info.ModTime()
	spec.NodeByName("gen_src").Params["c"] = "SRC2"

	res, err := eng.Build(work, Options{Spec: spec})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Success {
		t.Fatal("rebuild failed")
	}
	sm := statusMap(res)
	// gen_src -> merge -> hash 必须重建。
	for _, name := range []string{"gen_src", "merge", "hash"} {
		if sm[name].Status != StatusBuilt {
			t.Fatalf("node %s should rebuild, got %s (%v)", name, sm[name].Status, sm[name].Reasons)
		}
	}
	// 无关节点必须复用缓存。
	for _, name := range []string{"gen_hdr", "solo"} {
		if sm[name].Status != StatusCached {
			t.Fatalf("unrelated node %s should be cached, got %s (%v)", name, sm[name].Status, sm[name].Reasons)
		}
	}
	// 失效解释必须提到参数变化与传递依赖。
	if !containsAny(sm["gen_src"].Reasons, "参数变化") {
		t.Fatalf("gen_src reasons missing param change: %v", sm["gen_src"].Reasons)
	}
	if !containsAny(sm["merge"].Reasons, "依赖缓存键变化") {
		t.Fatalf("merge reasons missing dep-key change: %v", sm["merge"].Reasons)
	}
	if !containsAny(sm["hash"].Reasons, "依赖缓存键变化") {
		t.Fatalf("hash reasons missing dep-key change: %v", sm["hash"].Reasons)
	}
	// 新内容必须真的被重建出来。
	merged, _ := os.ReadFile(filepath.Join(work, "merged.txt"))
	if !strings.Contains(string(merged), "SRC2") {
		t.Fatalf("merged output does not reflect new content: %q", merged)
	}
	// mtime 保持旧值（证明系统只看内容；这里是修改参数重建，重建后 mtime 会更新，
	// 因此该校验只用于证明参数对比不依赖 mtime——直接比较文件内容即可）。
	_ = oldMtime
}

func TestInputFileContentChangeInvalidates(t *testing.T) {
	eng, work := newTestEngine(t)
	spec := &graph.Spec{
		Version: "1",
		Tools: map[string]*graph.Tool{
			"copy": {Name: "copy", ToolVersion: "1", Shell: true,
				Command: []string{"cp \"$1\" \"$2\""}},
		},
		Nodes: []*graph.Node{{
			Name: "cp", Tool: "copy",
			Inputs:  []string{"in.txt"},
			Outputs: []string{"out.txt"},
			Args:    []string{"in.txt", "out.txt"},
		}},
	}
	if err := os.WriteFile(filepath.Join(work, "in.txt"), []byte("one"), 0o644); err != nil {
		t.Fatal(err)
	}
	r1, _ := eng.Build(work, Options{Spec: spec})
	if !r1.Success {
		t.Fatal(r1.Error)
	}

	// touch mtime 但内容不变 => 命中。
	old := time.Now().Add(-10 * time.Hour)
	if err := os.Chtimes(filepath.Join(work, "in.txt"), old, old); err != nil {
		t.Fatal(err)
	}
	r2, _ := eng.Build(work, Options{Spec: spec})
	if statusMap(r2)["cp"].Status != StatusCached {
		t.Fatalf("mtime touch should be a hit: %v", statusMap(r2)["cp"].Reasons)
	}

	// 保持旧 mtime 但改内容 => 失效，且解释包含内容变化。
	if err := os.WriteFile(filepath.Join(work, "in.txt"), []byte("two"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(filepath.Join(work, "in.txt"), old, old); err != nil {
		t.Fatal(err)
	}
	r3, _ := eng.Build(work, Options{Spec: spec})
	nr := statusMap(r3)["cp"]
	if nr.Status != StatusBuilt {
		t.Fatalf("content change should rebuild, got %s", nr.Status)
	}
	if !containsAny(nr.Reasons, "输入内容变化") {
		t.Fatalf("reasons should mention input content change: %v", nr.Reasons)
	}
	out, _ := os.ReadFile(filepath.Join(work, "out.txt"))
	if string(out) != "two" {
		t.Fatalf("output not rebuilt from new content: %q", out)
	}
}

func TestToolVersionChangeInvalidates(t *testing.T) {
	eng, work := newTestEngine(t)
	spec := testSpec(t)
	if r, _ := eng.Build(work, Options{Spec: spec}); !r.Success {
		t.Fatal("first build failed")
	}
	spec.Tools["concat"].ToolVersion = "9.9.9"
	res, _ := eng.Build(work, Options{Spec: spec})
	sm := statusMap(res)
	if sm["merge"].Status != StatusBuilt {
		t.Fatalf("merge should rebuild on tool version bump, got %s", sm["merge"].Status)
	}
	if !containsAny(sm["merge"].Reasons, "工具版本变化") {
		t.Fatalf("reasons missing tool-version change: %v", sm["merge"].Reasons)
	}
	if sm["gen_src"].Status != StatusCached || sm["solo"].Status != StatusCached {
		t.Fatal("nodes using other tools must stay cached")
	}
}

func TestFailedNodeNotPublishedAndSkipsDependents(t *testing.T) {
	eng, work := newTestEngine(t)
	spec := testSpec(t)
	// 让 merge 失败：改用 fail 工具。
	spec.Tools["fail"].DeclaredEnv = nil
	spec.NodeByName("merge").Tool = "fail"
	spec.NodeByName("merge").Outputs = []string{"merged.txt"}

	res, err := eng.Build(work, Options{Spec: spec})
	if err != nil {
		t.Fatal(err)
	}
	if res.Success {
		t.Fatal("build should be reported unsuccessful")
	}
	sm := statusMap(res)
	if sm["merge"].Status != StatusFailed || sm["merge"].ExitCode != 7 {
		t.Fatalf("merge should fail with exit 7, got %+v", sm["merge"])
	}
	if !containsAny(sm["merge"].Reasons, "不发布缓存") {
		t.Fatalf("failed node reasons should state no cache publish: %v", sm["merge"].Reasons)
	}
	if sm["hash"].Status != StatusSkipped {
		t.Fatalf("hash should be skipped, got %s", sm["hash"].Status)
	}
	// 失败后再次构建：merge 必须重新执行（未发布缓存），且解释提到上次失败。
	spec2 := testSpec(t) // 恢复正常图，但 merge 仍应因“上次失败”而重新执行
	res2, err := eng.Build(work, Options{Spec: spec2})
	if err != nil {
		t.Fatal(err)
	}
	sm2 := statusMap(res2)
	if sm2["merge"].Status != StatusBuilt {
		t.Fatalf("merge should rebuild after previous failure, got %s (%v)", sm2["merge"].Status, sm2["merge"].Reasons)
	}
	if !res2.Success {
		t.Fatalf("recovered build should succeed: %s %v", res2.Error, sm2)
	}

	// 再来一轮：恢复后已成功，同键应正常命中（证明 prevFailed 只持续一次）。
	res3, err := eng.Build(work, Options{Spec: spec2})
	if err != nil {
		t.Fatal(err)
	}
	sm3 := statusMap(res3)
	if sm3["merge"].Status != StatusCached {
		t.Fatalf("merge should hit cache after recovery, got %s (%v)", sm3["merge"].Status, sm3["merge"].Reasons)
	}
}

func TestMissingOutputAfterSuccessIsFailure(t *testing.T) {
	eng, work := newTestEngine(t)
	spec := &graph.Spec{
		Version: "1",
		Tools: map[string]*graph.Tool{
			"noop": {Name: "noop", ToolVersion: "1", Shell: true, Command: []string{"true"}},
		},
		Nodes: []*graph.Node{{
			Name: "n", Tool: "noop", Outputs: []string{"should-exist.txt"},
		}},
	}
	res, _ := eng.Build(work, Options{Spec: spec})
	nr := statusMap(res)["n"]
	if nr.Status != StatusFailed || !containsAny(nr.Reasons, "输出缺失") {
		t.Fatalf("missing declared output should fail: %+v", nr)
	}
}

func TestCycleRejected(t *testing.T) {
	eng, work := newTestEngine(t)
	spec := testSpec(t)
	spec.NodeByName("gen_src").Deps = []string{"hash"} // gen_src -> merge -> hash -> gen_src
	res, err := eng.Build(work, Options{Spec: spec})
	if err != nil {
		t.Fatal(err)
	}
	if res.Success || len(res.Cycle) == 0 {
		t.Fatalf("cycle should be reported: %+v", res)
	}
}

func TestDryRunExplainsWithoutExecuting(t *testing.T) {
	eng, work := newTestEngine(t)
	spec := testSpec(t)
	res, err := eng.Build(work, Options{Spec: spec, DryRun: true})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Success {
		t.Fatal(res.Error)
	}
	for _, n := range res.Nodes {
		if n.Status != StatusBuilt {
			t.Fatalf("dry-run on empty cache should plan builds, got %s for %s", n.Status, n.Name)
		}
	}
	if _, err := os.Stat(filepath.Join(work, "src.txt")); !os.IsNotExist(err) {
		t.Fatal("dry-run must not create outputs")
	}
}

func TestStatePersistsAcrossEngineInstances(t *testing.T) {
	base := t.TempDir()
	work := filepath.Join(base, "work")
	cache := filepath.Join(base, "cache")
	state := filepath.Join(base, "state")
	_ = os.MkdirAll(work, 0o755)

	spec := testSpec(t)
	eng1, _ := New(cache, state, map[string]string{})
	r1, _ := eng1.Build(work, Options{Spec: spec})
	if !r1.Success {
		t.Fatal(r1.Error)
	}

	// 全新引擎实例（模拟服务重启/CLI 再次调用）：状态来自磁盘，全部命中。
	eng2, _ := New(cache, state, map[string]string{})
	r2, _ := eng2.Build(work, Options{Spec: spec})
	for _, n := range r2.Nodes {
		if n.Status != StatusCached {
			t.Fatalf("after restart node %s got %s: %v", n.Name, n.Status, n.Reasons)
		}
	}
}

func TestCacheAndWorkDirsAreSeparate(t *testing.T) {
	eng, work := newTestEngine(t)
	spec := testSpec(t)
	if r, _ := eng.Build(work, Options{Spec: spec}); !r.Success {
		t.Fatal(r.Error)
	}
	// 工作目录中只能出现夹具声明的文件，不应出现缓存/状态元数据。
	err := filepath.Walk(work, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		name := info.Name()
		if name == "meta.json" || name == "state.json" || strings.Contains(name, ".tmp") {
			return fmt.Errorf("cache/state artifact leaked into workdir: %s", path)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestResultsJSONSerializable(t *testing.T) {
	eng, work := newTestEngine(t)
	res, err := eng.Build(work, Options{Spec: testSpec(t)})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := json.Marshal(res); err != nil {
		t.Fatalf("result not JSON-serializable: %v", err)
	}
}

func containsAny(haystack []string, needle string) bool {
	for _, h := range haystack {
		if strings.Contains(h, needle) {
			return true
		}
	}
	return false
}

func errIsNil(t *testing.T, err error) bool {
	t.Helper()
	if err != nil {
		t.Fatal(err)
		return false
	}
	return true
}
