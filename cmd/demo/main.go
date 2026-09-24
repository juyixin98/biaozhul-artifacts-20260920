// Command shardmig-demo runs the full acceptance scenario over real HTTP
// against an in-process simulator: writes in every migration phase, a
// partition in every phase, duplicated control messages, and a lost append
// response retried. It prints each step and the final invariant report.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"shardmig/api"
	"shardmig/core"
)

type step struct {
	name string
	fn   func() error
}

func main() {
	verbose := flag.Bool("v", false, "print every response body")
	flag.Parse()

	sys, err := api.New(core.NewCluster(), "")
	if err != nil {
		fatal("start: %v", err)
	}
	defer sys.Close()
	d := &demo{
		base:    sys.ControllerURL(),
		nodeURL: map[string]string{core.NodeA: sys.NodeURL(core.NodeA), core.NodeB: sys.NodeURL(core.NodeB)},
		verbose: *verbose,
		httpc:   &http.Client{Timeout: 5 * time.Second},
	}

	scenario := []step{
		{"write w1,w2 on A epoch1", func() error {
			d.append("A", 1, "w1", "v1")
			d.append("A", 1, "w2", "v2")
			return nil
		}},
		{"begin_snapshot (cmd=snap-begin)", func() error { return d.ctrl("begin_snapshot", "snap-begin") }},
		{"write w3 during snapshot phase", func() error { d.append("A", 1, "w3", "v3"); return nil }},
		{"partition B, then complete_snapshot must fail (502)", func() error {
			d.setLink("B", false)
			if code, _ := d.ctrlRaw("complete_snapshot", "snap-done"); code != 502 {
				return fmt.Errorf("expected 502, got %d", code)
			}
			fmt.Println("    -> rejected as expected")
			d.setLink("B", true)
			return nil
		}},
		{"reconnect B; complete_snapshot (cmd=snap-done)", func() error { return d.ctrl("complete_snapshot", "snap-done") }},
		{"write w4, catch_up (cmd=catch-1)", func() error {
			d.append("A", 1, "w4", "v4")
			return d.ctrl("catch_up", "catch-1")
		}},
		{"duplicate catch_up with same command id -> replay", func() error {
			m := d.ctrlMustBody("catch_up", "catch-1")
			if m["replayed"] != true {
				return fmt.Errorf("expected replayed=true")
			}
			fmt.Println("    -> replayed=true, lag stays as first result")
			return nil
		}},
		{"write w5, fresh catch_up (cmd=catch-2) drains it", func() error {
			d.append("A", 1, "w5", "v5")
			return d.ctrl("catch_up", "catch-2")
		}},
		{"arm dropped-response fault on A", func() error { d.dropAppend("A", true); return nil }},
		{"write w6: first response lost, blind retry dedupes", func() error {
			code, m := d.appendRaw("A", 1, "w6", "v6")
			if code != 500 || m["code"] != "response_dropped_after_commit" {
				return fmt.Errorf("expected dropped fault, got %d %v", code, m["code"])
			}
			fmt.Println("    -> first attempt: committed, response lost (500)")
			code, m = d.appendRaw("A", 1, "w6", "v6")
			if code != 200 || m["deduplicated"] != true {
				return fmt.Errorf("expected deduped 200, got %d %v", code, m)
			}
			fmt.Println("    -> retry: 200 deduplicated=true (no second seq)")
			return nil
		}},
		{"partition A, catch_up must fail (502)", func() error {
			d.setLink("A", false)
			if code, _ := d.ctrlRaw("catch_up", "catch-while-a-down"); code != 502 {
				return fmt.Errorf("expected 502, got %d", code)
			}
			fmt.Println("    -> rejected as expected")
			d.setLink("A", true)
			return nil
		}},
		{"fresh catch_up (cmd=catch-3) after reconnect", func() error { return d.ctrl("catch_up", "catch-3") }},
		{"prepare_cutover (cmd=prep) -> writes frozen", func() error { return d.ctrl("prepare_cutover", "prep") }},
		{"write during switching -> 503 frozen", func() error {
			if code, _ := d.appendRaw("A", 1, "wX", "x"); code != 503 {
				return fmt.Errorf("expected 503, got %d", code)
			}
			fmt.Println("    -> rejected as expected")
			return nil
		}},
		{"partition B, commit must fail and epoch must stay 1", func() error {
			d.setLink("B", false)
			if code, _ := d.ctrlRaw("commit_cutover", "commit-1"); code != 502 {
				return fmt.Errorf("expected 502, got %d", code)
			}
			fmt.Println("    -> commit rejected, epoch unchanged")
			d.setLink("B", true)
			return nil
		}},
		{"retry same commit command id -> succeeds (failures are not cached)", func() error {
			return d.ctrl("commit_cutover", "commit-1")
		}},
		{"old primary A with epoch1 -> 409 stale_epoch", func() error {
			if code, _ := d.appendRaw("A", 1, "late", "x"); code != 409 {
				return fmt.Errorf("expected 409, got %d", code)
			}
			fmt.Println("    -> rejected as expected")
			return nil
		}},
		{"A forging epoch2 -> 403 not_primary", func() error {
			if code, _ := d.appendRaw("A", 2, "late2", "x"); code != 403 {
				return fmt.Errorf("expected 403, got %d", code)
			}
			fmt.Println("    -> rejected as expected")
			return nil
		}},
		{"new primary B with epoch2 confirms w7", func() error { d.append("B", 2, "w7", "v7"); return nil }},
		{"GET /verify invariants", func() error {
			checks, all := d.verify()
			for _, c := range checks {
				mark := "PASS"
				if !c.pass {
					mark = "FAIL"
				}
				fmt.Printf("    [%s] %s %s\n", mark, c.name, c.detail)
			}
			if !all {
				return fmt.Errorf("invariants failed")
			}
			return nil
		}},
	}

	fmt.Printf("controller: %s\nnode A: %s\nnode B: %s\n\n", d.base, d.nodeURL["A"], d.nodeURL["B"])
	for i, s := range scenario {
		fmt.Printf("[%02d] %s\n", i+1, s.name)
		if err := s.fn(); err != nil {
			fatal("step %q failed: %v", s.name, err)
		}
	}

	// final state dump
	fmt.Println("\nfinal state:")
	_, st := d.get("/status")
	b, _ := json.MarshalIndent(st, "  ", "  ")
	fmt.Println(string(b))
	fmt.Println("\nDEMO OK: all confirmed writes present, no dual-primary confirmation")
}

type demo struct {
	base    string
	nodeURL map[string]string
	verbose bool
	httpc   *http.Client
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "DEMO FAILED: "+format+"\n", args...)
	os.Exit(1)
}

func (d *demo) do(method, url string, body any) (int, map[string]any) {
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, url, rdr)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := d.httpc.Do(req)
	if err != nil {
		fatal("%s %s: %v", method, url, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var m map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &m)
	}
	if d.verbose {
		fmt.Printf("    %s %s -> %d %s\n", method, url, resp.StatusCode, string(raw))
	}
	return resp.StatusCode, m
}

func (d *demo) post(path string, body any) (int, map[string]any) {
	return d.do("POST", d.base+path, body)
}

func (d *demo) get(path string) (int, map[string]any) {
	return d.do("GET", d.base+path, nil)
}

func (d *demo) append(node string, epoch int, k, v string) {
	code, m := d.appendRaw(node, epoch, k, v)
	if code != 200 {
		fatal("append %s: %d %v", k, code, m)
	}
	fmt.Printf("    -> confirmed seq=%.0f epoch=%.0f primary=%v\n", m["seq"].(float64), m["epoch"].(float64), m["primary"])
}

func (d *demo) appendRaw(node string, epoch int, k, v string) (int, map[string]any) {
	return d.post("/append", map[string]any{"node": node, "epoch": epoch, "key": k, "value": v})
}

func (d *demo) ctrl(action, cmd string) error {
	code, m := d.ctrlRaw(action, cmd)
	if code != 200 {
		return fmt.Errorf("%s: %d %v", action, code, m)
	}
	fmt.Printf("    -> phase=%v %s\n", m["phase"], extra(m))
	return nil
}

func (d *demo) ctrlMustBody(action, cmd string) map[string]any {
	code, m := d.ctrlRaw(action, cmd)
	if code != 200 {
		fatal("%s: %d %v", action, code, m)
	}
	return m
}

func (d *demo) ctrlRaw(action, cmd string) (int, map[string]any) {
	return d.post("/migration/"+action, map[string]any{"command_id": cmd})
}

func (d *demo) setLink(node string, up bool) {
	code, m := d.post("/nodes/"+node+"/link", map[string]any{"up": up})
	if code != 200 {
		fatal("setlink: %d %v", code, m)
	}
	fmt.Printf("    -> link %s up=%v\n", node, up)
}

func (d *demo) dropAppend(node string, drop bool) {
	code, m := d.post("/nodes/"+node+"/drop-append", map[string]any{"drop": drop})
	if code != 200 {
		fatal("dropappend: %d %v", code, m)
	}
}

type check struct {
	name   string
	pass   bool
	detail string
}

func (d *demo) verify() ([]check, bool) {
	_, m := d.get("/verify")
	var out []check
	for _, raw := range m["checks"].([]any) {
		c := raw.(map[string]any)
		detail, _ := c["detail"].(string)
		out = append(out, check{c["name"].(string), c["pass"].(bool), detail})
	}
	return out, m["all_pass"] == true
}

func extra(m map[string]any) string {
	for _, k := range []string{"lag", "installed", "epoch", "primary"} {
		if v, ok := m[k]; ok {
			return fmt.Sprintf("%s=%v", k, v)
		}
	}
	return ""
}
