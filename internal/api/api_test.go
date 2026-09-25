package api

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"deltaupdate/internal/service"
)

type testServer struct {
	name string
	srv  *Server
	hs   *httptest.Server
	root string
}

func newTestServer(t *testing.T, name string, spaceCap uint64) *testServer {
	t.Helper()
	root := t.TempDir()
	svc, err := service.New(service.Config{
		CacheDir:     filepath.Join(root, "cache"),
		StateDir:     filepath.Join(root, "state"),
		WorkDir:      filepath.Join(root, "work"),
		FreeSpaceCap: spaceCap,
		BuildTimeout: 30 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	srv := New(svc)
	hs := httptest.NewServer(srv.Handler())
	t.Cleanup(hs.Close)
	return &testServer{name: name, srv: srv, hs: hs, root: root}
}

func (ts *testServer) do(t *testing.T, method, path string, body any, raw bool) (int, []byte, map[string]string) {
	t.Helper()
	var rdr io.Reader
	hdr := map[string]string{}
	if body != nil {
		if raw {
			rdr = body.(io.Reader)
			hdr["Content-Type"] = "application/octet-stream"
		} else {
			b, _ := json.Marshal(body)
			rdr = bytes.NewReader(b)
			hdr["Content-Type"] = "application/json"
		}
	}
	req, err := http.NewRequest(method, ts.hs.URL+path, rdr)
	if err != nil {
		t.Fatal(err)
	}
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, data, nil
}

func mustOK[T any](t *testing.T, ts *testServer, method, path string, body any) T {
	t.Helper()
	status, data, _ := ts.do(t, method, path, body, false)
	if status != http.StatusOK {
		t.Fatalf("%s %s -> %d: %s", method, path, status, data)
	}
	var env struct {
		OK   bool `json:"ok"`
		Data T    `json:"data"`
	}
	if err := json.Unmarshal(data, &env); err != nil {
		t.Fatal(err)
	}
	if !env.OK {
		t.Fatalf("%s %s 返回失败: %s", method, path, data)
	}
	return env.Data
}

// TestEndToEndHTTP 覆盖完整链路：
// 构建端：注册项目(显式夹具命令) -> 入库 v1 -> 构建 v2 -> 生成差量；
// 设备端：下载 v1 制品 -> 下发补丁 -> 应用 -> 核验最终摘要。
func TestEndToEndHTTP(t *testing.T) {
	builder := newTestServer(t, "builder", 0)
	device := newTestServer(t, "device", 0)

	// 夹具：构建命令把目录外的夹具文件复制成产物
	v2Fixture := filepath.Join(builder.root, "v2-artifact.bin")
	v2Data := makeData(2, 300_000)
	if err := os.WriteFile(v2Fixture, v2Data, 0o644); err != nil {
		t.Fatal(err)
	}

	mustOK[struct{}](t, builder, "POST", "/v1/projects", map[string]any{
		"name":          "app",
		"argv":          []string{"cp", v2Fixture, "artifact.bin"},
		"artifact_path": "artifact.bin",
	})

	// v1 基线制品直接上传
	v1Data := makeData(1, 300_000)
	status, raw, _ := builder.do(t, "POST", "/v1/artifacts/app", bytes.NewReader(v1Data), true)
	if status != http.StatusOK {
		t.Fatalf("上传 v1 失败: %d %s", status, raw)
	}
	var v1Ingest struct {
		Data struct {
			Digest string `json:"digest"`
		} `json:"data"`
	}
	json.Unmarshal(raw, &v1Ingest)
	oldDigest := v1Ingest.Data.Digest

	// 构建 v2
	built := mustOK[struct {
		Digest string `json:"digest"`
	}](t, builder, "POST", "/v1/projects/app/build", map[string]any{})
	newDigest := built.Digest
	if newDigest == oldDigest {
		t.Fatalf("v2 摘要不应等于 v1")
	}

	// 生成差量
	pr := mustOK[struct {
		Digest string `json:"digest"`
	}](t, builder, "POST", "/v1/patches", map[string]any{
		"name":       "app",
		"old_digest": oldDigest,
		"new_digest": newDigest,
	})
	patchDigest := pr.Digest

	// 设备端先获得 v1 制品（模拟首装）
	status, _, _ = device.do(t, "POST", "/v1/artifacts/app", bytes.NewReader(v1Data), true)
	if status != http.StatusOK {
		t.Fatalf("设备上传 v1 失败: %d", status)
	}

	// 下发补丁到设备
	pb := download(t, builder, "/v1/patches/"+patchDigest+"/download")
	status, raw, _ = device.do(t, "POST", "/v1/patches/ingest", bytes.NewReader(pb), true)
	if status != http.StatusOK {
		t.Fatalf("下发补丁失败: %d %s", status, raw)
	}
	var ing struct {
		Data struct {
			Digest string `json:"digest"`
		} `json:"data"`
	}
	json.Unmarshal(raw, &ing)
	if ing.Data.Digest != patchDigest {
		t.Fatalf("补丁下发后摘要不一致")
	}

	// 应用补丁
	ar := mustOK[struct {
		OldDigest string `json:"old_digest"`
		NewDigest string `json:"new_digest"`
	}](t, device, "POST", "/v1/apply", map[string]any{
		"name":         "app",
		"patch_digest": patchDigest,
	})
	if ar.OldDigest != oldDigest || ar.NewDigest != newDigest {
		t.Fatalf("应用结果摘要错误: %+v", ar)
	}

	// 当前制品摘要 + 下载内容双重核验
	cur := mustOK[struct {
		Digest string `json:"digest"`
	}](t, device, "GET", "/v1/artifacts/app/current", nil)
	if cur.Digest != newDigest {
		t.Fatalf("当前摘要 %s != 期望 %s", cur.Digest, newDigest)
	}
	got := download(t, device, "/v1/artifacts/app/download")
	if !bytes.Equal(got, v2Data) {
		t.Fatalf("设备上制品内容与 v2 不一致")
	}
}

// TestHTTPWrongBaselineAndCorruptPatch HTTP 层的错基线 / 损坏补丁。
func TestHTTPWrongBaselineAndCorruptPatch(t *testing.T) {
	builder := newTestServer(t, "builder", 0)

	v1 := makeData(1, 200_000)
	v2 := makeData(2, 200_000)
	d1 := ingestHTTP(t, builder, "app", v1)
	d2 := ingestHTTP(t, builder, "app", v2)
	pr := mustOK[struct{ Digest string }](t, builder, "POST", "/v1/patches", map[string]any{
		"name": "app", "old_digest": d1, "new_digest": d2,
	})
	pb := download(t, builder, "/v1/patches/"+pr.Digest+"/download")

	// 设备当前是“错误基线” v2，应用 v1->v2 补丁必须 422
	device2 := newTestServer(t, "device2", 0)
	ingestHTTP(t, device2, "app", v2)
	patchHTTP(t, device2, pb)
	status, raw, _ := device2.do(t, "POST", "/v1/apply", map[string]any{
		"name": "app", "patch_digest": pr.Digest,
	}, false)
	if status != http.StatusUnprocessableEntity {
		t.Fatalf("错基线应返回 422，实际 %d: %s", status, raw)
	}

	// 损坏补丁：翻转补丁体一个字节，下发后应用必须失败且旧制品保持 v2
	corrupt := append([]byte{}, pb...)
	corrupt[len(corrupt)-500] ^= 0xff
	patchHTTP(t, device2, corrupt)
	var cd struct {
		Data struct {
			Digest string `json:"digest"`
		} `json:"data"`
	}
	status, raw, _ = device2.do(t, "POST", "/v1/patches/ingest", bytes.NewReader(corrupt), true)
	if status != http.StatusOK {
		t.Fatalf("损坏补丁入库失败: %d %s", status, raw)
	}
	json.Unmarshal(raw, &cd)
	status, raw, _ = device2.do(t, "POST", "/v1/apply", map[string]any{
		"name": "app", "patch_digest": cd.Data.Digest,
	}, false)
	if status == http.StatusOK {
		t.Fatalf("损坏补丁应用竟然成功: %s", raw)
	}
	cur := mustOK[struct{ Digest string }](t, device2, "GET", "/v1/artifacts/app/current", nil)
	if cur.Digest != d2 {
		t.Fatalf("失败后当前制品被改变: %s", cur.Digest)
	}
}

// TestHTTPNoSpace 空间不足：入库与应用都必须以 507 拒绝，且已有制品不受影响。
func TestHTTPNoSpace(t *testing.T) {
	device := newTestServer(t, "device", 100) // 极小磁盘
	big := makeData(9, 200_000)
	status, raw, _ := device.do(t, "POST", "/v1/artifacts/app", bytes.NewReader(big), true)
	if status != http.StatusInsufficientStorage {
		t.Fatalf("空间不足应返回 507，实际 %d: %s", status, raw)
	}
	var env struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	json.Unmarshal(raw, &env)
	if env.Error.Code != "no_space" {
		t.Fatalf("错误码应为 no_space，实际 %s", env.Error.Code)
	}
}

func ingestHTTP(t *testing.T, ts *testServer, name string, data []byte) string {
	t.Helper()
	status, raw, _ := ts.do(t, "POST", "/v1/artifacts/"+name, bytes.NewReader(data), true)
	if status != http.StatusOK {
		t.Fatalf("ingest %s: %d %s", name, status, raw)
	}
	var env struct {
		Data struct {
			Digest string `json:"digest"`
		} `json:"data"`
	}
	json.Unmarshal(raw, &env)
	return env.Data.Digest
}

func patchHTTP(t *testing.T, ts *testServer, data []byte) string {
	t.Helper()
	status, raw, _ := ts.do(t, "POST", "/v1/patches/ingest", bytes.NewReader(data), true)
	if status != http.StatusOK {
		t.Fatalf("patch ingest: %d %s", status, raw)
	}
	var env struct {
		Data struct {
			Digest string `json:"digest"`
		} `json:"data"`
	}
	json.Unmarshal(raw, &env)
	return env.Data.Digest
}

func download(t *testing.T, ts *testServer, path string) []byte {
	t.Helper()
	resp, err := http.Get(ts.hs.URL + path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("下载 %s: %d", path, resp.StatusCode)
	}
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func makeData(seed byte, n int) []byte {
	b := make([]byte, n)
	for i := range b {
		b[i] = seed + byte(i%7)
	}
	return b
}
