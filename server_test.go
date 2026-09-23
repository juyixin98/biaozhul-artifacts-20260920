package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

// HTTP 端到端：起 3 个真实 HTTP 副本，模拟分区与恢复，
// 通过 /sync 接口收敛，验证最终集合一致。
func TestHTTPThreeReplicaPartitionHeal(t *testing.T) {
	nodes := make([]*httptest.Server, 3)
	for i := range nodes {
		srv := NewServer(fmt.Sprintf("node-%d", i))
		nodes[i] = httptest.NewServer(srv.routes())
		defer nodes[i].Close()
	}
	url := func(i int) string { return nodes[i].URL }

	// 初始：node0 添加 shared，同步到所有副本
	httpAdd(t, url(0), "shared")
	httpSync(t, url(0), url(1))
	httpSync(t, url(0), url(2))

	// --- 分区：node0 离线，node1<->node2 互通 ---
	httpAdd(t, url(0), "offline-0") // node0 离线写
	httpRemove(t, url(0), "shared") // node0 删除它观察到的 shared
	httpAdd(t, url(1), "online-1")
	httpAdd(t, url(2), "online-2")
	httpRemove(t, url(2), "shared") // node2 也删除 shared
	httpSync(t, url(1), url(2))     // 分区内互通

	// 分区期间各副本视图确实不同
	if got := httpElements(t, url(0)); reflect.DeepEqual(got, httpElements(t, url(1))) {
		t.Fatal("during partition, node0 and node1 should have diverged views")
	}

	// --- 恢复：两两同步到不动点 ---
	for round := 0; round < 2; round++ {
		httpSync(t, url(0), url(1))
		httpSync(t, url(1), url(2))
		httpSync(t, url(0), url(2))
	}

	want := []string{"offline-0", "online-1", "online-2"}
	for i := range nodes {
		if got := httpElements(t, url(i)); !reflect.DeepEqual(got, want) {
			t.Fatalf("node %d elements = %v, want %v", i, got, want)
		}
	}

	// 重复同步（幂等性）：再同步一轮，结果不变
	httpSync(t, url(0), url(1))
	if got := httpElements(t, url(0)); !reflect.DeepEqual(got, want) {
		t.Fatalf("after duplicate sync, node 0 elements = %v, want %v", got, want)
	}
}

// HTTP 层并发增加/删除冒烟测试（配合 -race）。
func TestHTTPConcurrentOps(t *testing.T) {
	srv := httptest.NewServer(NewServer("solo").routes())
	defer srv.Close()

	done := make(chan struct{})
	for g := 0; g < 4; g++ {
		go func(g int) {
			for i := 0; i < 50; i++ {
				e := fmt.Sprintf("item-%d", (g+i)%7)
				httpAdd(t, srv.URL, e)
				if i%2 == 0 {
					httpRemove(t, srv.URL, e)
				}
			}
			done <- struct{}{}
		}(g)
	}
	for g := 0; g < 4; g++ {
		<-done
	}
	// 服务仍然健康响应即可
	httpElements(t, srv.URL)
}

func httpAdd(t *testing.T, base, element string) {
	t.Helper()
	body := post(t, base+"/add", `{"element":"`+element+`"}`)
	var resp struct {
		Tag Tag `json:"tag"`
	}
	if err := json.Unmarshal([]byte(body), &resp); err != nil || resp.Tag == "" {
		t.Fatalf("add %q: bad response %s", element, body)
	}
}

func httpRemove(t *testing.T, base, element string) {
	post(t, base+"/remove", `{"element":"`+element+`"}`)
}

func httpSync(t *testing.T, from, peer string) {
	post(t, from+"/sync", `{"peer":"`+peer+`"}`)
}

func httpElements(t *testing.T, base string) []string {
	t.Helper()
	resp, err := http.Get(base + "/elements")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var body struct {
		Elements []string `json:"elements"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	return body.Elements
}

func post(t *testing.T, url, body string) string {
	t.Helper()
	resp, err := http.Post(url, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var sb strings.Builder
	buf := make([]byte, 4096)
	for {
		n, err := resp.Body.Read(buf)
		sb.Write(buf[:n])
		if err != nil {
			break
		}
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("POST %s -> %d: %s", url, resp.StatusCode, sb.String())
	}
	return sb.String()
}
