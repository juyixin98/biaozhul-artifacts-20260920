// Command handcheck 打印 README §手算验证 中两条序列的逐区间明细，
// 供人工对照（不依赖 HTTP，直接调用核心计算库）。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"counterreset/internal/rate"
	"counterreset/internal/series"
)

const base = int64(1700000000000)

func mk(secs []int, vals []float64) []series.Sample {
	s := make([]series.Sample, len(secs))
	for i := range secs {
		s[i] = series.Sample{TimestampMs: base + int64(secs[i])*1000, Value: vals[i]}
	}
	return s
}

func main() {
	capacity := flag.Float64("capacity", 100, "条件上界使用的计数器容量 C；<=0 表示不提供")
	flag.Parse()

	cases := []struct {
		name string
		secs []int
		vals []float64
	}{
		{
			name: "A 单次重置 + 缺样",
			secs: []int{0, 10, 20, 30, 50, 60, 70, 80, 90, 100},
			vals: []float64{0, 10, 20, 20, 30, 40, 45, 60, 10, 15},
		},
		{
			name: "B 两次重置",
			secs: []int{0, 10, 20, 30, 40, 50},
			vals: []float64{0, 10, 2, 8, 1, 5},
		},
	}

	for _, c := range cases {
		samples := mk(c.secs, c.vals)
		start := samples[0].TimestampMs
		end := samples[len(samples)-1].TimestampMs
		r, err := rate.Compute(samples, start, end, rate.ExtrapNone, *capacity)
		if err != nil {
			fmt.Fprintf(os.Stderr, "%s 计算失败: %v\n", c.name, err)
			os.Exit(1)
		}
		secOf := func(tsMs int64) int { return int((tsMs - base) / 1000) }
		fmt.Printf("\n########## 序列 %s（t 秒: %v；v: %v）##########\n", c.name, c.secs, c.vals)
		fmt.Printf("%-12s %-7s %-12s %-13s %-8s %s\n",
			"interval", "dt(s)", "values", "increase_lb", "ub(C)", "marks")
		var sumLb, sumUb float64
		hasUb := false
		for _, iv := range r.Intervals {
			marks := ""
			if iv.Reset {
				marks += "RESET "
			}
			if iv.IsGap {
				marks += fmt.Sprintf("GAP(%.1fx)", iv.GapRatio)
			}
			ub := "-"
			if iv.IncreaseUb != nil {
				ub = fmt.Sprintf("%.0f", *iv.IncreaseUb)
				sumUb += *iv.IncreaseUb
				hasUb = true
			}
			fmt.Printf("%4d→%-7d %-7d %5.0f→%-6.0f %-13.0f %-8s %s\n",
				secOf(iv.T1Ms), secOf(iv.T2Ms),
				iv.DeltaMs/1000, iv.V1, iv.V2, iv.IncreaseLb, ub, marks)
			sumLb += iv.IncreaseLb
		}
		fmt.Printf("合计：下界点估计 = %.0f", sumLb)
		if hasUb {
			fmt.Printf("；条件上界（C=%.0f 且每区间至多一次重置）= %.0f", *capacity, sumUb)
		} else {
			fmt.Print("；未提供容量 → 真实增量无上界（upper=null）")
		}
		fmt.Printf("；窗口 %ds；速率下界 = %.4f/s；重置 %d 次；缺样 %d 个\n",
			r.WindowMs/1000, r.RatePerSecond.Point, len(r.Resets), len(r.Gaps))
		raw, _ := json.Marshal(map[string]any{"point": r.Increase.Point, "upper": r.Increase.Upper, "rate": r.RatePerSecond.Point})
		fmt.Printf("JSON %s\n", raw)
	}
}
