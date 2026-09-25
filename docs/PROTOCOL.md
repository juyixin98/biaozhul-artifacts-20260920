# 协议时序与崩溃窗口

```
处理线程            CheckpointStore          OutputLog(staged→committed)        SummaryTable
   |  屏障(每N条/时间/手动)
   |------------------|
   | ①写 checkpoint-N.tmp (fsync)
   |        ····· 故障点 STATE_WRITE：tmp 留存，恢复时删除并整体重放（不丢数据）
   |  rename → checkpoint-N.json
   |----------------------------------->|
   |                      ②写 txn-N.staged (fsync, 不可见)
   |                               ····· 故障点 OUTPUT_STAGE：staged 丢弃，重放重建
   |                      ③rename staged → txn-N.json （提交点）
   |                               ····· 故障点 OUTPUT_COMMIT：提交已生效；
   |                                     若 checkpoint-N 已在而 txn-N 缺失，回滚到 N-1 重放
   |--------------------------------------------------------------------->|
   |                                          ④由全部已提交事务 fold 重建 summary + 原子重写
   |                                                          ····· 故障点 TABLE_APPLY：无害，重启重建
   v
推进内部水位 (lastCheckpointId=N, sinceCheckpoint=0)
```

## 恢复判定：完整检查点

```
K = max { i | checkpoint-i.json 存在 且 output/txn-i.json 已提交 }
删除所有编号 > K 的孤儿 checkpoint 文件；删除所有 *.staged / *.tmp
状态恢复到 checkpoint-K；从 input.log 的 offset=K.nextOffset 开始重放
summary.json 始终由已提交事务集合 fold 重建（不信任旧文件内容）
```

## 各故障点的“看到什么”

| 故障点 | checkpoint-N | txn-N | summary 是否含本批 | 恢复动作 |
|---|---|---|---|---|
| STATE_WRITE | 仅 .tmp，无正式文件 | 无 | 否 | 从 N-1 重放 |
| OUTPUT_STAGE | 有（孤儿） | 仅 .staged | 否 | 删孤儿检查点与 staged，从 N-1 重放 |
| OUTPUT_COMMIT | 有 | rename 已完成→已提交 | 尚未更新 | 检查点与事务均在，从 N 继续；summary 重建补齐 |
| TABLE_APPLY | 有 | 已提交 | 可能已/未更新 | 从 N 继续；summary 无条件重建 |
