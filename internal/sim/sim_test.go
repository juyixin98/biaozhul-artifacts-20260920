package sim

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// parseT 解析内联脚本，测试失败时立即中止。
func parseT(t *testing.T, raw string) *Script {
	t.Helper()
	s, err := ParseScript([]byte(raw))
	if err != nil {
		t.Fatalf("脚本解析失败: %v\n%s", err, raw)
	}
	return s
}

func TestBasicElectionAndWrite(t *testing.T) {
	s := parseT(t, `{
	  "seed": 1, "ticks": 30,
	  "node_windows": [{"node":0,"lo":4,"hi":5},{"node":1,"lo":8,"hi":9},{"node":2,"lo":10,"hi":11}],
	  "events": [
	    {"at": 8, "kind": "client_write", "client": "c1", "data": "hello"},
	    {"at": 20, "kind": "committed", "client": "c1", "data": "hello", "expect_index": 2},
	    {"at": 21, "kind": "log_match"}
	  ]
	}`)
	r := Run(s)
	if !r.OK {
		t.Fatalf("基本选举+写入应通过，失败: %v", r.Failures)
	}
	if len(r.TermHistory) == 0 {
		t.Fatal("结果中没有任何 leader 记录")
	}
}

func TestDeterminism(t *testing.T) {
	s := parseT(t, `{
	  "seed": 314, "ticks": 80,
	  "network": {"delay_ticks": 1, "jitter": 4, "loss_rate": 0.1, "dup_rate": 0.05},
	  "events": [
	    {"at": 5,  "kind": "isolate", "isolated": 0},
	    {"at": 30, "kind": "isolate", "isolated": 0, "heal": true},
	    {"at": 35, "kind": "client_write", "client": "a", "data": "d1"},
	    {"at": 75, "kind": "log_match"}
	  ]
	}`)
	r1 := RunWithTrace(s, 1000)
	r2 := RunWithTrace(s, 1000)
	b1, _ := json.Marshal(r1)
	b2, _ := json.Marshal(r2)
	if string(b1) != string(b2) {
		t.Fatal("相同脚本两次运行结果不一致（非确定性）")
	}
}

func TestMajorityLostNoAck(t *testing.T) {
	s := parseT(t, `{
	  "seed": 5, "ticks": 45,
	  "node_windows": [{"node":0,"lo":4,"hi":5},{"node":1,"lo":8,"hi":9},{"node":2,"lo":10,"hi":11}],
	  "events": [
	    {"at": 7, "kind": "crash", "target": 1},
	    {"at": 7, "kind": "crash", "target": 2},
	    {"at": 12, "kind": "client_write", "client": "w", "data": "blocked", "node": 0},
	    {"at": 30, "kind": "not_acked", "client": "w"},
	    {"at": 30, "kind": "commit_index", "target": 0, "expect_index": 0},
	    {"at": 32, "kind": "restart", "target": 1},
	    {"at": 32, "kind": "restart", "target": 2},
	    {"at": 42, "kind": "committed", "client": "w", "data": "blocked", "expect_index": 2}
	  ]
	}`)
	r := Run(s)
	if !r.OK {
		t.Fatalf("多数派失联不确认、恢复后确认的场景应通过: %v", r.Failures)
	}
}

// TestFileStorageAcrossProcesses 用两次独立的 Run 模拟两个"进程"：
// 第一次把已提交数据写入真实文件并崩溃；第二次复用同一目录启动，
// 必须从磁盘恢复任期、选票与日志。
func TestFileStorageAcrossProcesses(t *testing.T) {
	dir := t.TempDir()
	first := parseT(t, `{
	  "seed": 9, "ticks": 20,
	  "storage": "file", "state_dir": "`+dir+`",
	  "node_windows": [{"node":0,"lo":4,"hi":5},{"node":1,"lo":8,"hi":9},{"node":2,"lo":10,"hi":11}],
	  "events": [
	    {"at": 7, "kind": "client_write", "client": "k", "data": "durable"},
	    {"at": 15, "kind": "committed", "client": "k", "data": "durable", "expect_index": 2},
	    {"at": 16, "kind": "crash", "target": 0},
	    {"at": 16, "kind": "crash", "target": 1},
	    {"at": 16, "kind": "crash", "target": 2}
	  ]
	}`)
	r1 := Run(first)
	if !r1.OK {
		t.Fatalf("第一阶段（写入并持久化）失败: %v", r1.Failures)
	}
	// 磁盘文件必须存在且含 durable。
	for id := 0; id < 3; id++ {
		raw, err := os.ReadFile(filepath.Join(dir, "node"+string(rune('0'+id)), "raft-state.json"))
		if err != nil {
			t.Fatalf("节点 %d 持久化文件不存在: %v", id, err)
		}
		if !strings.Contains(string(raw), "durable") {
			t.Fatalf("节点 %d 持久化文件不含已提交数据", id)
		}
	}
	// 第二次"进程"：全新 Engine，从磁盘恢复。
	second := parseT(t, `{
	  "seed": 11, "ticks": 45,
	  "storage": "file", "state_dir": "`+dir+`",
	  "node_windows": [{"node":0,"lo":6,"hi":7},{"node":1,"lo":8,"hi":9},{"node":2,"lo":10,"hi":11}],
	  "events": [
	    {"at": 0,  "kind": "log_contains", "target": 0, "data": "durable", "full_log": true},
	    {"at": 0,  "kind": "log_contains", "target": 1, "data": "durable", "full_log": true},
	    {"at": 0,  "kind": "log_contains", "target": 2, "data": "durable", "full_log": true},
	    {"at": 40, "kind": "client_write", "client": "k2", "data": "after-restart"},
	    {"at": 44, "kind": "log_contains", "target": 0, "data": "durable after-restart", "full_log": true}
	  ]
	}`)
	r2 := Run(second)
	if !r2.OK {
		t.Fatalf("第二阶段（跨进程恢复）失败: %v", r2.Failures)
	}
	// 重启后各节点日志必须仍含 durable（其索引位置因追加 noop 会后移），任期不回退。
	for _, n := range r2.Nodes {
		found := false
		for _, e := range n.FullLog {
			if e.Data == "durable" {
				found = true
			}
		}
		if !found {
			t.Fatalf("节点 %d 恢复后丢失已提交数据 durable: %v", n.ID, n.FullLog)
		}
		if n.Term < 1 {
			t.Fatalf("节点 %d 重启后任期回退: %d", n.ID, n.Term)
		}
	}
}

func TestWipeLosesState(t *testing.T) {
	s := parseT(t, `{
	  "seed": 21, "ticks": 50,
	  "node_windows": [{"node":0,"lo":4,"hi":5},{"node":1,"lo":8,"hi":9},{"node":2,"lo":10,"hi":11}],
	  "events": [
	    {"at": 7, "kind": "client_write", "client": "k", "data": "persisted"},
	    {"at": 14, "kind": "committed", "client": "k", "data": "persisted", "expect_index": 2},
	    {"at": 20, "kind": "crash", "target": 2},
	    {"at": 21, "kind": "wipe", "target": 2},
	    {"at": 23, "kind": "restart", "target": 2},
	    {"at": 48, "kind": "log_contains", "target": 2, "data": "persisted", "full_log": true}
	  ]
	}`)
	r := Run(s)
	if !r.OK {
		t.Fatalf("清盘节点重启后应通过追赶重新获得日志: %v", r.Failures)
	}
	node2 := r.Nodes[2]
	if len(node2.FullLog) == 0 {
		t.Fatal("节点 2 重启追赶后日志仍为空")
	}
}

func TestRejectsInvalidScript(t *testing.T) {
	cases := []string{
		`{"ticks": 10, "nodes": 5}`,                                  // 非 3 节点
		`{"ticks": 0}`,                                               // ticks 非法
		`{"ticks": 10, "storage": "network"}`,                        // 非法 storage
		`{"ticks": 10, "events": [{"at": 1, "kind": "frobnicate"}]}`, // 未知事件
		`{not json`, // JSON 语法错误
	}
	for i, raw := range cases {
		if _, err := ParseScript([]byte(raw)); err == nil {
			t.Fatalf("用例 %d 应当被拒绝: %s", i, raw)
		}
	}
}
