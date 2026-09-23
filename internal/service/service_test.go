package service

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"depscanner/internal/builder"
)

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func builderBuild(s1 *ScanResponse, store builder.Store) (*builder.Result, error) {
	root := s1.Root
	return builder.Build(builder.Options{
		Root:       root,
		Targets:    append([]string(nil), s1.Graph.Targets...),
		QuoteDirs:  nil,
		SystemDirs: nil,
	}, store)
}

func setupIncProject(t *testing.T) (root, cache string) {
	t.Helper()
	root = t.TempDir()
	cache = filepath.Join(t.TempDir(), "cache")
	writeFile(t, filepath.Join(root, "src/main.c"), `#include "hdr/version.h"
#include "hdr/util.h"
`)
	writeFile(t, filepath.Join(root, "src/hdr/version.h"), `#ifndef VERSION_H
#define VERSION_H
#include "base.h"
#endif
`)
	writeFile(t, filepath.Join(root, "src/hdr/util.h"), `#ifndef UTIL_H
#define UTIL_H
#include "base.h"
#endif
`)
	writeFile(t, filepath.Join(root, "src/hdr/base.h"), `#define BASE 1
`)
	// 第二个独立目标
	writeFile(t, filepath.Join(root, "src/other.c"), `#include "hdr/base.h"
`)
	return root, cache
}

func baselineReq(root, cache string) ScanRequest {
	return ScanRequest{
		Root:     root,
		Targets:  []string{"src/main.c", "src/other.c"},
		CacheDir: cache,
	}
}

func TestScanThenAffected_NoChange(t *testing.T) {
	root, cache := setupIncProject(t)
	eng := Engine{}

	scan, err := eng.Scan(baselineReq(root, cache))
	if err != nil {
		t.Fatal(err)
	}
	if scan.Status != "ok" {
		t.Fatalf("scan status = %s: %+v", scan.Status, scan.Diagnostics)
	}
	if scan.Baseline.Existed {
		t.Error("first scan baseline should not pre-exist")
	}

	aff, err := eng.Affected(AffectedRequest{ScanRequest: baselineReq(root, cache)})
	if err != nil {
		t.Fatal(err)
	}
	if aff.ConfigChanged {
		t.Error("config should be unchanged")
	}
	if len(aff.Changed.Added)+len(aff.Changed.Modified)+len(aff.Changed.Deleted) != 0 {
		t.Errorf("expected no changes, got %+v", aff.Changed)
	}
	if len(aff.AffectedTargets) != 0 {
		t.Errorf("expected no affected targets, got %v", aff.AffectedTargets)
	}
}

func TestAffected_ModifySharedHeader(t *testing.T) {
	root, cache := setupIncProject(t)
	eng := Engine{}

	if _, err := eng.Scan(baselineReq(root, cache)); err != nil {
		t.Fatal(err)
	}
	// 两个目标都（直接或间接）依赖 base.h
	writeFile(t, filepath.Join(root, "src/hdr/base.h"), `#define BASE 2
`)

	aff, err := eng.Affected(AffectedRequest{ScanRequest: baselineReq(root, cache)})
	if err != nil {
		t.Fatal(err)
	}
	if len(aff.Changed.Modified) != 1 || aff.Changed.Modified[0] != "src/hdr/base.h" {
		t.Fatalf("modified = %v", aff.Changed.Modified)
	}
	got := map[string]bool{}
	for _, x := range aff.AffectedFiles {
		got[x] = true
	}
	for _, want := range []string{"src/main.c", "src/other.c", "src/hdr/base.h",
		"src/hdr/version.h", "src/hdr/util.h"} {
		if !got[want] {
			t.Errorf("affected files missing %s: %v", want, aff.AffectedFiles)
		}
	}
	targets := map[string]bool{}
	for _, x := range aff.AffectedTargets {
		targets[x] = true
	}
	if !targets["src/main.c"] || !targets["src/other.c"] {
		t.Errorf("both targets should be affected: %v", aff.AffectedTargets)
	}
}

func TestAffected_ModifyExclusiveHeader(t *testing.T) {
	root, cache := setupIncProject(t)
	eng := Engine{}
	if _, err := eng.Scan(baselineReq(root, cache)); err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(root, "src/hdr/util.h"), `#ifndef UTIL_H
#define UTIL_H
#include "base.h"
#define EXTRA 1
#endif
`)
	aff, err := eng.Affected(AffectedRequest{ScanRequest: baselineReq(root, cache)})
	if err != nil {
		t.Fatal(err)
	}
	if len(aff.Changed.Modified) != 1 {
		t.Fatalf("modified = %v", aff.Changed.Modified)
	}
	if len(aff.AffectedTargets) != 1 || aff.AffectedTargets[0] != "src/main.c" {
		t.Fatalf("only main.c should be affected, got %v", aff.AffectedTargets)
	}
}

func TestAffected_DeleteHeader(t *testing.T) {
	root, cache := setupIncProject(t)
	eng := Engine{}
	if _, err := eng.Scan(baselineReq(root, cache)); err != nil {
		t.Fatal(err)
	}

	if err := os.Remove(filepath.Join(root, "src/hdr/version.h")); err != nil {
		t.Fatal(err)
	}
	aff, err := eng.Affected(AffectedRequest{ScanRequest: baselineReq(root, cache)})
	if err != nil {
		t.Fatal(err)
	}
	if len(aff.Changed.Deleted) != 1 || aff.Changed.Deleted[0] != "src/hdr/version.h" {
		t.Fatalf("deleted = %v", aff.Changed.Deleted)
	}
	// 删除的节点仍出现在 affected 文件集合里
	found := false
	for _, p := range aff.AffectedFiles {
		if p == "src/hdr/version.h" {
			found = true
		}
	}
	if !found {
		t.Errorf("deleted file should be listed among affected files: %v", aff.AffectedFiles)
	}
	if len(aff.AffectedTargets) != 1 || aff.AffectedTargets[0] != "src/main.c" {
		t.Fatalf("only main.c should be affected by deletion, got %v", aff.AffectedTargets)
	}
	if aff.Status != "ok" {
		t.Fatalf("missing includes are warnings, status = %s", aff.Status)
	}
}

func TestAffected_ModifyThenDeleteAgainstSameBaseline(t *testing.T) {
	// 基线只建立一次：先改一个文件，再删除另一个文件，两次只读分析之间不落基线。
	// 删除必须仍能在“基线那张图”上反向追溯到目标。
	root, cache := setupIncProject(t)
	eng := Engine{}
	if _, err := eng.Scan(baselineReq(root, cache)); err != nil {
		t.Fatal(err)
	}

	// 第一次变更：改 util.h（只影响 main.c）
	writeFile(t, filepath.Join(root, "src/hdr/util.h"), "#define UTIL_H\n#define U 2\n")
	aff1, err := eng.Affected(AffectedRequest{ScanRequest: baselineReq(root, cache)})
	if err != nil {
		t.Fatal(err)
	}
	if len(aff1.AffectedTargets) != 1 || aff1.AffectedTargets[0] != "src/main.c" {
		t.Fatalf("step1 targets = %v", aff1.AffectedTargets)
	}

	// 第二次变更（基线未刷新）：再删除 version.h，仍应追溯到 main.c。
	if err := os.Remove(filepath.Join(root, "src/hdr/version.h")); err != nil {
		t.Fatal(err)
	}
	aff2, err := eng.Affected(AffectedRequest{ScanRequest: baselineReq(root, cache)})
	if err != nil {
		t.Fatal(err)
	}
	if len(aff2.Changed.Deleted) != 1 || aff2.Changed.Deleted[0] != "src/hdr/version.h" {
		t.Fatalf("step2 deleted = %v", aff2.Changed.Deleted)
	}
	if len(aff2.AffectedTargets) != 1 || aff2.AffectedTargets[0] != "src/main.c" {
		t.Fatalf("step2 targets = %v, want [src/main.c]", aff2.AffectedTargets)
	}
}

func TestAffected_AddHeader(t *testing.T) {
	root, cache := setupIncProject(t)
	eng := Engine{}
	if _, err := eng.Scan(baselineReq(root, cache)); err != nil {
		t.Fatal(err)
	}
	// main.c 增加对一个新头的引用
	writeFile(t, filepath.Join(root, "src/new.h"), `#define NEW 1
`)
	writeFile(t, filepath.Join(root, "src/main.c"), `#include "hdr/version.h"
#include "hdr/util.h"
#include "new.h"
`)
	aff, err := eng.Affected(AffectedRequest{ScanRequest: baselineReq(root, cache)})
	if err != nil {
		t.Fatal(err)
	}
	if len(aff.Changed.Added) != 1 || aff.Changed.Added[0] != "src/new.h" {
		t.Fatalf("added = %v", aff.Changed.Added)
	}
	if len(aff.Changed.Modified) != 1 || aff.Changed.Modified[0] != "src/main.c" {
		t.Fatalf("modified = %v", aff.Changed.Modified)
	}
	if len(aff.AffectedTargets) != 1 || aff.AffectedTargets[0] != "src/main.c" {
		t.Fatalf("only main.c target should be affected, got %v", aff.AffectedTargets)
	}
}

func TestAffected_ConfigChangeAffectsAll(t *testing.T) {
	root, cache := setupIncProject(t)
	eng := Engine{}
	if _, err := eng.Scan(baselineReq(root, cache)); err != nil {
		t.Fatal(err)
	}
	req := baselineReq(root, cache)
	req.Targets = []string{"src/main.c"} // 目标集合变化
	aff, err := eng.Affected(AffectedRequest{ScanRequest: req})
	if err != nil {
		t.Fatal(err)
	}
	if !aff.ConfigChanged {
		t.Fatal("expected config_changed=true")
	}
	if len(aff.AffectedTargets) != 1 || aff.AffectedTargets[0] != "src/main.c" {
		t.Fatalf("all targets should be affected, got %v", aff.AffectedTargets)
	}
}

func TestAffected_NoBaselineTreatsAllAsAffected(t *testing.T) {
	root, cache := setupIncProject(t)
	aff, err := Engine{}.Affected(AffectedRequest{ScanRequest: baselineReq(root, cache)})
	if err != nil {
		t.Fatal(err)
	}
	if aff.Baseline.Existed {
		t.Error("baseline should not exist")
	}
	if !aff.ConfigChanged {
		t.Error("missing baseline implies config_changed=true")
	}
	if len(aff.AffectedTargets) != 2 {
		t.Errorf("all targets affected, got %v", aff.AffectedTargets)
	}
}

func TestCacheDirMustBeOutsideRoot(t *testing.T) {
	root, _ := setupIncProject(t)
	eng := Engine{}
	req := baselineReq(root, filepath.Join(root, "cache-inside"))
	if _, err := eng.Scan(req); err == nil {
		t.Fatal("cache inside root must be rejected")
	}
	req2 := ScanRequest{Root: root, Targets: []string{"src/main.c"}, CacheDir: root}
	if _, err := eng.Scan(req2); err == nil {
		t.Fatal("cache == root must be rejected")
	}
}

func TestIncrementalReadReusesCache(t *testing.T) {
	root, cache := setupIncProject(t)
	eng := Engine{}
	s1, err := eng.Scan(baselineReq(root, cache))
	if err != nil {
		t.Fatal(err)
	}
	if s1.Graph.Stats.FilesRead == 0 {
		t.Fatal("first scan must read files")
	}
	// 跨进程（快照往返后单调时钟丢失）：快路径缓存命中为 0，
	// 但每个文件只做一次内容 hash 校验，不再执行词法解析。
	s2, err := eng.Scan(baselineReq(root, cache))
	if err != nil {
		t.Fatal(err)
	}
	if s2.Graph.Stats.FilesRead != 0 {
		t.Errorf("unchanged files should not be re-parsed, got %d reads", s2.Graph.Stats.FilesRead)
	}
	if s2.Graph.Stats.HashesChecked != s1.Graph.Stats.FilesTotal {
		t.Errorf("hash checks = %d, total = %d", s2.Graph.Stats.HashesChecked, s1.Graph.Stats.FilesTotal)
	}
}

func TestIncrementalInProcessFastCache(t *testing.T) {
	// 同进程内连续两次 Build 且文件不变：mtime 单调时钟可信，走快路径。
	root, cache := setupIncProject(t)
	eng := Engine{}
	s1, err := eng.Scan(baselineReq(root, cache))
	if err != nil {
		t.Fatal(err)
	}
	snap, err := loadSnapshot(snapshotPath(cache, root))
	if err != nil {
		t.Fatal(err)
	}
	store := newMemStore()
	for p, f := range snap.Files {
		f.MTimeTrusted = true
		// 用一次真实 stat 拿回带单调时钟的 mtime
		if fi, statErr := os.Stat(f.Abs); statErr == nil {
			f.ModTime = fi.ModTime()
		}
		store.files[p] = f
	}
	res, err := builderBuild(s1, store)
	if err != nil {
		t.Fatal(err)
	}
	if res.Stats.CacheHits != s1.Graph.Stats.FilesTotal {
		t.Errorf("fast-path hits = %d, want %d", res.Stats.CacheHits, s1.Graph.Stats.FilesTotal)
	}
}

func TestParseEndpoint(t *testing.T) {
	root, _ := setupIncProject(t)
	resp, err := Engine{}.ParseFile(ParseRequest{Root: root, File: "src/main.c"})
	if err != nil {
		t.Fatal(err)
	}
	if resp.Status != "ok" || len(resp.Includes) != 2 {
		t.Fatalf("parse resp = %+v", resp)
	}
}

func TestHTTP_ScanAndAffected(t *testing.T) {
	root, cache := setupIncProject(t)
	srv := httptest.NewServer(Handler())
	defer srv.Close()

	post := func(path string, body any) (int, map[string]any) {
		data, _ := json.Marshal(body)
		resp, err := http.Post(srv.URL+path, "application/json", bytes.NewReader(data))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out map[string]any
		if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
			t.Fatal(err)
		}
		return resp.StatusCode, out
	}

	code, out := post("/api/v1/scan", baselineReq(root, cache))
	if code != http.StatusOK {
		t.Fatalf("scan code=%d body=%v", code, out)
	}
	if out["status"] != "ok" {
		t.Errorf("status = %v", out["status"])
	}

	code, out = post("/api/v1/affected", baselineReq(root, cache))
	if code != http.StatusOK {
		t.Fatalf("affected code=%d", code)
	}
	if _, ok := out["affected_targets"]; !ok {
		t.Errorf("missing affected_targets: %v", out)
	}
}

func TestHTTP_BadRequestAndHealth(t *testing.T) {
	srv := httptest.NewServer(Handler())
	defer srv.Close()

	// 未知字段 -> 400
	resp, err := http.Post(srv.URL+"/api/v1/scan", "application/json",
		bytes.NewReader([]byte(`{"root":"x","bogus":1}`)))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("unknown field: code=%d, want 400", resp.StatusCode)
	}

	// 非法 JSON -> 400
	resp, err = http.Post(srv.URL+"/api/v1/scan", "application/json",
		bytes.NewReader([]byte(`{not json`)))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("bad json: code=%d", resp.StatusCode)
	}

	// 健康检查
	resp, err = http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("healthz code=%d", resp.StatusCode)
	}
}

func TestHTTP_MacroIncludeStatusOKButGraphError(t *testing.T) {
	root := t.TempDir()
	cache := filepath.Join(t.TempDir(), "cache")
	writeFile(t, filepath.Join(root, "m.c"), "#include MACRO\n")
	srv := httptest.NewServer(Handler())
	defer srv.Close()

	body, _ := json.Marshal(ScanRequest{
		Root: root, Targets: []string{"m.c"}, CacheDir: cache,
	})
	resp, err := http.Post(srv.URL+"/api/v1/scan", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	// 请求本身合法 -> 200，结果 status=error
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("code=%d, want 200", resp.StatusCode)
	}
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if out["status"] != "error" {
		t.Errorf("status = %v, want error", out["status"])
	}
}
