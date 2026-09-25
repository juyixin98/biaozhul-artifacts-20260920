// Command probe 是故障注入客户端的命令行演示：
// 启动一个进程内假服务，并对其发起带退避重试的调用，打印结构化结果。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"contractcheck/internal/clock"
	"contractcheck/internal/faultclient"
)

func main() {
	kind := flag.String("fault", "none", "故障类型: none|delay|status|badbody|close|flaky")
	status := flag.Int("status", 500, "fault=status 时返回的状态码")
	flaky := flag.Int("flaky-times", 2, "fault=flaky 时前几次失败")
	attempts := flag.Int("attempts", 3, "最大尝试次数（含首次）")
	backoff := flag.Duration("backoff", 20*time.Millisecond, "初始退避")
	maxBackoff := flag.Duration("max-backoff", 500*time.Millisecond, "退避上限")
	fakeTime := flag.Bool("fake-time", false, "使用可控时钟与假睡（退避不占真实时间）")
	flag.Parse()

	spec := faultclient.FaultSpec{
		Kind:       faultclient.FaultKind(*kind),
		StatusCode: *status,
		FlakyTimes: *flaky,
	}
	server := faultclient.NewFakeServer(spec)
	defer server.Close()

	cfg := faultclient.Config{
		MaxAttempts:    *attempts,
		InitialBackoff: *backoff,
		MaxBackoff:     *maxBackoff,
	}

	clk := clock.Clock(clock.Real{})
	var sleeper faultclient.Sleeper = faultclient.RealSleeper{}
	if *fakeTime {
		fake := clock.NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
		clk = fake
		sleeper = faultclient.NewFakeSleeper(fake)
	}

	c := faultclient.New(cfg, clk, sleeper)
	result := c.Get(server.URL)

	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(result); err != nil {
		fmt.Fprintln(os.Stderr, "输出失败:", err)
		os.Exit(1)
	}
	if !result.Success {
		os.Exit(2)
	}
}
