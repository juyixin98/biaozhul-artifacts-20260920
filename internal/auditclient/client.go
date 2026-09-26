// Package auditclient 是业务服务调用"外部审计系统"的瘦客户端。
// 调用走真实 HTTP（loopback 上的本进程假服务），带超时与状态码检查。
package auditclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"idempotentsave/internal/fakeaudit"
)

// Client 审计系统客户端。
type Client struct {
	baseURL string
	http    *http.Client
}

// New 创建客户端，baseURL 例如 http://127.0.0.1:9100。
func New(baseURL string) *Client {
	return &Client{
		baseURL: baseURL,
		http:    &http.Client{Timeout: 5 * time.Second},
	}
}

// Record 向审计系统写入一条事件。非 2xx 都作为错误返回，
// 调用方无法区分"事件未处理"与"事件已处理但响应丢失"——这正是演示要点。
func (c *Client) Record(ctx context.Context, e fakeaudit.Event) error {
	body, err := json.Marshal(e)
	if err != nil {
		return fmt.Errorf("marshal audit event: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/audit/events", bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build audit request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("call audit system: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("audit system returned status %d", resp.StatusCode)
	}
	return nil
}
