package analyzer

import "fmt"

// DemoSpans 返回 README 中手算用的串并行 DAG（时间单位：微秒，相对起点 0）。
//
//	root     [0,300)   根 span
//	 ├ write [10,200)  同步依赖：root 发起后一直等待
//	 │  ├ shard-a [50,130)  同步写分片（wal 为其同步子调用）
//	 │  │  └ wal   [60,90)  同步落盘
//	 │  └ shard-b [100,180) async 并行写分片
//	 └ gc     [210,260) async 后台 GC
//
// 串行屏障规则：write 的同步孩子只有 shard-a（结束于 130），屏障=130；
// shard-b 越过屏障的尾部 [130,180) 上关键路径，[100,130) 与 shard-a 并行被吸收。
// gc 完全在 root 结束前完成，不上关键路径，全部为并行余量。
func DemoSpans(traceID string) []Span {
	if traceID == "" {
		traceID = "demo-trace-1"
	}
	return []Span{
		{TraceID: traceID, SpanID: "root", Name: "request", StartUs: 0, EndUs: 300},
		{TraceID: traceID, SpanID: "write", ParentID: "root", Name: "db.write", StartUs: 10, EndUs: 200},
		{TraceID: traceID, SpanID: "shard-a", ParentID: "write", Name: "db.shard-a", StartUs: 50, EndUs: 130},
		{TraceID: traceID, SpanID: "wal", ParentID: "shard-a", Name: "db.wal-fsync", StartUs: 60, EndUs: 90},
		{TraceID: traceID, SpanID: "shard-b", ParentID: "write", Name: "db.shard-b", StartUs: 100, EndUs: 180, Async: true},
		{TraceID: traceID, SpanID: "gc", ParentID: "root", Name: "bg.gc", StartUs: 210, EndUs: 260, Async: true},
	}
}

// OverlappingSpans 构造“同步兄弟重叠”的时钟矛盾样例。
func OverlappingSpans(traceID string) []Span {
	if traceID == "" {
		traceID = "overlap-trace"
	}
	return []Span{
		{TraceID: traceID, SpanID: "r", Name: "root", StartUs: 0, EndUs: 100},
		{TraceID: traceID, SpanID: "a", ParentID: "r", Name: "sync-a", StartUs: 10, EndUs: 60},
		{TraceID: traceID, SpanID: "b", ParentID: "r", Name: "sync-b", StartUs: 40, EndUs: 90},
	}
}

// CycleSpans 构造循环 parent 引用：a -> c -> b -> a。
func CycleSpans(traceID string) []Span {
	if traceID == "" {
		traceID = "cycle-trace"
	}
	return []Span{
		{TraceID: traceID, SpanID: "a", ParentID: "c", Name: "a", StartUs: 0, EndUs: 10},
		{TraceID: traceID, SpanID: "b", ParentID: "a", Name: "b", StartUs: 1, EndUs: 9},
		{TraceID: traceID, SpanID: "c", ParentID: "b", Name: "c", StartUs: 2, EndUs: 8},
	}
}

// MissingParentSpans 构造缺 span + 时钟越界样例：
// ghost 引用不存在的父（孤儿提升）；late 结束时间越过父 root。
func MissingParentSpans(traceID string) []Span {
	if traceID == "" {
		traceID = "missing-trace"
	}
	return []Span{
		{TraceID: traceID, SpanID: "r", Name: "root", StartUs: 0, EndUs: 100},
		{TraceID: traceID, SpanID: "ghost", ParentID: "does-not-exist", Name: "orphan", StartUs: 200, EndUs: 250},
		{TraceID: traceID, SpanID: "late", ParentID: "r", Name: "overrun-child", StartUs: 80, EndUs: 140},
	}
}

// NestedOverlapSpans 构造孩子之间互相重叠的样例，用于验证自耗时并集去重：
// root[0,100) 下 x[10,60) 与 y[40,90) 重叠 [40,60)。
// naive 求和会扣 (50+50)=100；并集长度只有 80，自耗时应为 20。
func NestedOverlapSpans(traceID string) []Span {
	if traceID == "" {
		traceID = "selfoverlap-trace"
	}
	return []Span{
		{TraceID: traceID, SpanID: "r", Name: "root", StartUs: 0, EndUs: 100},
		{TraceID: traceID, SpanID: "x", ParentID: "r", Name: "x", StartUs: 10, EndUs: 60, Async: true},
		{TraceID: traceID, SpanID: "y", ParentID: "r", Name: "y", StartUs: 40, EndUs: 90, Async: true},
	}
}

// RandomishSpans 生成一个确定性的多层串并行 trace（非随机，保证样例可复现）。
// auth 为同步串行前序；fan 下第一个 shard 同步、其余 async 并行；
// log 为 root 下的 async 审计日志。
func RandomishSpans(traceID string, nShards int) []Span {
	if traceID == "" {
		traceID = "synth-trace"
	}
	if nShards < 1 {
		nShards = 1
	}
	var fanEnd int64 = 30
	shardSpans := []Span{}
	for i := 0; i < nShards; i++ {
		start := int64(40 + i*15)
		end := start + int64(60+(i%3)*20)
		shardSpans = append(shardSpans, Span{
			TraceID: traceID, SpanID: fmt.Sprintf("shard-%d", i), ParentID: "fan",
			Name: fmt.Sprintf("shard.call-%d", i), StartUs: start, EndUs: end,
			Async: i != 0,
		})
		if end > fanEnd {
			fanEnd = end
		}
	}
	fanEnd += 10
	spans := []Span{
		{TraceID: traceID, SpanID: "root", Name: "http.handle", StartUs: 0, EndUs: fanEnd + 40},
		{TraceID: traceID, SpanID: "auth", ParentID: "root", Name: "auth.check", StartUs: 5, EndUs: 25},
		{TraceID: traceID, SpanID: "fan", ParentID: "root", Name: "fanout", StartUs: 30, EndUs: fanEnd},
	}
	spans = append(spans, shardSpans...)
	spans = append(spans, Span{
		TraceID: traceID, SpanID: "log", ParentID: "root", Name: "async.audit-log",
		StartUs: 35, EndUs: fanEnd - 20, Async: true,
	})
	return spans
}
