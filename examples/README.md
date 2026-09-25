# 请求样例

- `run_samples.sh`：对本地服务依次发起健康检查、策略查看、夹具列表、
  9 个判定请求和 2 个夹具执行请求。需要先启动服务：

  ```bash
  go run ./cmd/licensejudge        # 终端 A
  ./examples/run_samples.sh       # 终端 B
  ```

  可用 `BASE_URL` 覆盖地址，`OUT_DIR` 覆盖响应输出目录。
- `requests/`：每个 POST 的 JSON 请求体。
- `responses/`：一次真实运行保存的响应（作为输出样例；可随时重新生成）。

所有样例只访问 `127.0.0.1`，不涉及外部网络。
