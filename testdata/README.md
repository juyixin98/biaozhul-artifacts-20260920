# testdata — 示例输入

- `trusted_sample.json`：离线生成的**可信样例链**（`cmd/genchain` 产物）。包含可信创世哈希、
  目标 tip 高度、tip 哈希与整链 digest。桩节点用它对外服务，同步器只用它做信任锚点与最终比对。
  可用 `go run ./cmd/genchain --length 32` 重新生成（哈希是真实 SHA-256 计算结果）。
- `node_alpha.json` / `node_beta.json` / `node_gamma.json`：三个桩节点的行为配置。

## 故障模型（字段：`faults[].type` + 高度窗口 `start..end`，请求区间与之重叠即生效）

| 节点 | advertised_delta | 故障 |
| --- | --- | --- |
| alpha | `+7`（虚报高度） | `timeout` 于 9..16：挂起直到客户端超时，绝不返回 |
| beta  | `0` | `corrupt` 于 4..7：翻转块体字节却不重算哈希 → SHA-256 自哈希不符 |
| gamma | `-2`（低报高度） | `badparent` 于 17：从 17 起把父哈希换成 `0xEE..EE` 并重算，形成**段内自洽的分叉**，只能在可信锚点边界被拒 |

另有可选字段 `delay_ms`：`{ "<请求起始高度>": 毫秒 }`，用于慢节点/超时模拟。

同步器对每段按尝试轮换节点（`(start + attempt) % n`），因此三类坏段都会先命中故障源、
记录证据，再由其它诚实源修复；缺口之前的连续前缀照常发布，缺口本身不可跳过。
