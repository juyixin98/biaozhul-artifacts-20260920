// Command seed 向运行中的 metricsink 服务 POST 一批合成样本。
//
// 用法示例：
//
//	go run ./cmd/seed -density=ragged -hours=3 -seed=42
//	go run ./cmd/seed -density=dense  -hours=3 -seed=7
//	go run ./cmd/seed -density=gappy  -hours=3 -seed=9
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"time"

	"metricsink/internal/model"
	"metricsink/internal/synth"
)

func main() {
	base := flag.String("addr", "http://127.0.0.1:8080", "服务地址")
	metric := flag.String("metric", "cpu.usage", "指标名")
	host := flag.String("host", "host-a", "host 标签值")
	density := flag.String("density", "ragged", "dense|sparse|ragged|gappy")
	hours := flag.Int64("hours", 3, "生成多少小时的数据")
	seed := flag.Int64("seed", 42, "随机种子（可复现）")
	endNow := flag.Bool("end-now", false, "数据结束于当前时刻；默认结束于最近整点前")
	flag.Parse()

	end := time.Now().Unix()
	if !*endNow {
		// 对齐到整点边界：end 取最近一个整点 - 1，保证跨小时边界且可复现。
		now := time.Now().UTC()
		end = now.Truncate(time.Hour).Unix() - 1
	}
	start := end - *hours*3600 + 1

	opts := synth.Options{
		Metric:   *metric,
		Labels:   map[string]string{"host": *host},
		IDPrefix: fmt.Sprintf("%s-%s-s%d", *host, *density, *seed),
		Start:    start,
		End:      end,
		Density:  synth.Density(*density),
		Seed:     *seed,
	}
	samples := synth.Generate(opts)

	body, err := json.Marshal(struct {
		Samples []model.Sample `json:"samples"`
	}{samples})
	if err != nil {
		log.Fatal(err)
	}
	resp, err := http.Post(*base+"/v1/ingest", "application/json", bytes.NewReader(body))
	if err != nil {
		log.Fatalf("请求失败: %v", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	fmt.Printf("density=%s seed=%d 样本数=%d 时间范围=%s ~ %s\nHTTP %d\n%s\n",
		*density, *seed, len(samples),
		model.FormatTs(start), model.FormatTs(end),
		resp.StatusCode, string(raw))
}
