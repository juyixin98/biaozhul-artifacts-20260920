package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// buildCLI 编译当前命令包，返回二进制路径。
func buildCLI(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	bin := filepath.Join(dir, "simsnap")
	root, _ := filepath.Abs("../..")
	cmd := exec.Command("go", "build", "-o", bin, "./cmd/simsnap")
	cmd.Dir = root
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("编译CLI失败: %v\n%s", err, out)
	}
	return bin
}

// TestCLIFileIO：-in 读样例、-out 写结果文件，JSON 可解析且 FIFO 快照守恒。
func TestCLIFileIO(t *testing.T) {
	bin := buildCLI(t)
	work := t.TempDir()
	out := filepath.Join(work, "resp.json")

	root, _ := filepath.Abs(".." + string(os.PathSeparator) + "..")
	in := filepath.Join(root, "examples", "03-overlapping.json")

	cmd := exec.Command(bin, "-in", in, "-out", out)
	if o, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("CLI执行失败: %v\n%s", err, o)
	}
	raw, err := os.ReadFile(out)
	if err != nil {
		t.Fatalf("未生成输出文件: %v", err)
	}
	var resp map[string]any
	if err := json.Unmarshal(raw, &resp); err != nil {
		t.Fatalf("输出不是合法JSON: %v", err)
	}
	snaps, ok := resp["snapshots"].([]any)
	if !ok || len(snaps) != 3 {
		t.Fatalf("应有3个快照结果, got %v", resp["snapshots"])
	}
	for _, s := range snaps {
		sr := s.(map[string]any)
		if sr["conserved"] != true {
			t.Fatalf("快照%v 未守恒: %v", sr["snapshot"], sr)
		}
	}
}

// TestCLIStdin：通过标准输入喂 JSON，结果写到标准输出。
func TestCLIStdin(t *testing.T) {
	bin := buildCLI(t)
	req := `{"seed":1,"balances":[50,50],"default_policy":{"min_delay":2,"max_delay":3,"fifo":true},` +
		`"transfers":[{"at":1,"from":0,"to":1,"amount":20}],"snapshots":[{"at":20,"id":1,"initiator":0}],"deadline":60}`
	cmd := exec.Command(bin)
	cmd.Stdin = strings.NewReader(req)
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("stdin模式执行失败: %v", err)
	}
	var resp struct {
		FinalTotal int `json:"final_total"`
		Snapshots  []struct {
			Conserved bool `json:"conserved"`
		} `json:"snapshots"`
	}
	if err := json.Unmarshal(out, &resp); err != nil {
		t.Fatalf("输出非JSON: %v", err)
	}
	if resp.FinalTotal != 100 || len(resp.Snapshots) != 1 || !resp.Snapshots[0].Conserved {
		t.Fatalf("CLI stdin 结果错误: %+v", resp)
	}
}

// TestCLIRejectsBadJSON：非法输入以非零码退出并给出错误。
func TestCLIRejectsBadJSON(t *testing.T) {
	bin := buildCLI(t)
	cmd := exec.Command(bin)
	cmd.Stdin = strings.NewReader("{not json")
	if err := cmd.Run(); err == nil {
		t.Fatal("非法JSON应导致非零退出码")
	}
}
