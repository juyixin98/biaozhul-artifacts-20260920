package telemetry

import (
	"bytes"
	"encoding/json"
	"fmt"
	"regexp"
	"time"
)

// Payload 是设备遥测消息的业务载荷。
// 业务事件键 = (DeviceID, BootGen, Seq):
//   - DeviceID:设备标识
//   - BootGen:启动代次,设备每次重启单调递增,序号随之从 1 重新计数
//   - Seq:本代次内的采样序号
type Payload struct {
	DeviceID    *string   `json:"device_id"`
	BootGen     *int64    `json:"boot_gen"`
	Seq         *int64    `json:"seq"`
	SampledAt   time.Time `json:"sampled_at"`
	Temperature *float64  `json:"temperature"`
	Humidity    *float64  `json:"humidity"`
}

var deviceIDRe = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// ParsePayload 解析并校验业务载荷。任何字段缺失或越界都返回错误,
// 调用方应把原始消息送入隔离区而不是丢弃。
func ParsePayload(b []byte) (*Payload, error) {
	var p Payload
	dec := json.NewDecoder(bytes.NewReader(b))
	if err := dec.Decode(&p); err != nil {
		return nil, fmt.Errorf("JSON 解析失败: %w", err)
	}
	if p.DeviceID == nil || !deviceIDRe.MatchString(*p.DeviceID) {
		return nil, fmt.Errorf("device_id 缺失或非法(需 1-64 位 [A-Za-z0-9_-])")
	}
	if p.BootGen == nil || *p.BootGen < 1 {
		return nil, fmt.Errorf("boot_gen 缺失或非法(需 >= 1)")
	}
	if p.Seq == nil || *p.Seq < 1 {
		return nil, fmt.Errorf("seq 缺失或非法(需 >= 1)")
	}
	if p.SampledAt.IsZero() {
		return nil, fmt.Errorf("sampled_at 缺失或非法(需 RFC3339 时间)")
	}
	if p.Temperature == nil || *p.Temperature < -100 || *p.Temperature > 150 {
		return nil, fmt.Errorf("temperature 缺失或越界(需 [-100, 150])")
	}
	if p.Humidity == nil || *p.Humidity < 0 || *p.Humidity > 1 {
		return nil, fmt.Errorf("humidity 缺失或越界(需 [0, 1])")
	}
	return &p, nil
}
