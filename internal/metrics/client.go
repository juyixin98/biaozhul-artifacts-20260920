// Package metrics 是指标桩服务的 HTTP 客户端。
package metrics

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"time"

	"rollout/internal/engine"
)

type Client struct {
	BaseURL string
	HTTP    *http.Client
}

func New(baseURL string) *Client {
	return &Client{
		BaseURL: baseURL,
		HTTP:    &http.Client{Timeout: 5 * time.Second},
	}
}

// Window 拉取桩服务在 [from,to) 区间的指标。HTTP 错误或 204 均返回 nil 窗口，
// 由引擎判为「指标缺失 → 未知」，而不是健康。
func (c *Client) Window(ctx context.Context, service string, from, to time.Time) (*engine.MetricsWindow, error) {
	u := fmt.Sprintf("%s/metrics?service=%s&from=%s&to=%s",
		c.BaseURL,
		url.QueryEscape(service),
		url.QueryEscape(from.UTC().Format(time.RFC3339)),
		url.QueryEscape(to.UTC().Format(time.RFC3339)),
	)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNoContent {
		return nil, nil
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("metrics stub returned %d", resp.StatusCode)
	}
	var w engine.MetricsWindow
	if err := json.NewDecoder(resp.Body).Decode(&w); err != nil {
		return nil, err
	}
	return &w, nil
}
