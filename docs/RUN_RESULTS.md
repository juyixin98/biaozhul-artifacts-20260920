# 测试运行记录（2026-09-24, go1.23.4 linux/amd64）

```
$ go test -race -count=1 ./...
?   	quorum-demo/cmd/server	[no test files]
ok  	quorum-demo/api	1.069s
ok  	quorum-demo/quorum	1.067s
```

## demo.sh 场景摘要

- 场景一（部分写超时）：504 acked=[n1] -> 恢复 -> 读 200 且 repaired_to=[n2,n3]
- 场景二（并发写）：409 conflict，兄弟版本 A{ n1:1} 与 B{n2:1} -> resolve winner {n1:1,n2:1,n3:1} -> 读 200 唯一版本
- 场景三（副本恢复）：n3 错过写 -> /repair updated=[n3]，再次 repair updated=[]（幂等）
- 对照（W=1,R=1）：写仅落 n1，读 n2 返回 404（法定人数不相交），读 n1 返回 200

完整 JSON 输出可用 ./demo.sh 随时复现（版本 ID 含随机后缀，每次不同）。
