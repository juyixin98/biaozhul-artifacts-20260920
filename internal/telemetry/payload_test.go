package telemetry

import "testing"

func validJSON() string {
	return `{"device_id":"dev-001","boot_gen":1,"seq":7,"sampled_at":"2026-09-23T10:00:00Z","temperature":21.5,"humidity":0.45}`
}

func TestParsePayload_Valid(t *testing.T) {
	p, err := ParsePayload([]byte(validJSON()))
	if err != nil {
		t.Fatalf("合法载荷被拒绝: %v", err)
	}
	if *p.DeviceID != "dev-001" || *p.BootGen != 1 || *p.Seq != 7 {
		t.Fatalf("业务键解析错误: %+v", p)
	}
	if *p.Temperature != 21.5 || *p.Humidity != 0.45 {
		t.Fatalf("量测值解析错误: %+v", p)
	}
}

func TestParsePayload_Invalid(t *testing.T) {
	cases := map[string]string{
		"非JSON":       `not-json`,
		"截断的JSON":     `{"device_id":"dev-001",`,
		"缺device_id":  `{"boot_gen":1,"seq":1,"sampled_at":"2026-09-23T10:00:00Z","temperature":20,"humidity":0.5}`,
		"非法device_id": `{"device_id":"bad id!","boot_gen":1,"seq":1,"sampled_at":"2026-09-23T10:00:00Z","temperature":20,"humidity":0.5}`,
		"缺boot_gen":   `{"device_id":"d","seq":1,"sampled_at":"2026-09-23T10:00:00Z","temperature":20,"humidity":0.5}`,
		"boot_gen为0":  `{"device_id":"d","boot_gen":0,"seq":1,"sampled_at":"2026-09-23T10:00:00Z","temperature":20,"humidity":0.5}`,
		"缺seq":        `{"device_id":"d","boot_gen":1,"sampled_at":"2026-09-23T10:00:00Z","temperature":20,"humidity":0.5}`,
		"seq为负":       `{"device_id":"d","boot_gen":1,"seq":-1,"sampled_at":"2026-09-23T10:00:00Z","temperature":20,"humidity":0.5}`,
		"缺时间":         `{"device_id":"d","boot_gen":1,"seq":1,"temperature":20,"humidity":0.5}`,
		"温度越界":        `{"device_id":"d","boot_gen":1,"seq":1,"sampled_at":"2026-09-23T10:00:00Z","temperature":200,"humidity":0.5}`,
		"湿度越界":        `{"device_id":"d","boot_gen":1,"seq":1,"sampled_at":"2026-09-23T10:00:00Z","temperature":20,"humidity":1.5}`,
	}
	for name, body := range cases {
		if _, err := ParsePayload([]byte(body)); err == nil {
			t.Errorf("%s: 应被拒绝却通过了", name)
		}
	}
}

func TestParsePayload_AllowsExtraFields(t *testing.T) {
	body := `{"device_id":"d","boot_gen":1,"seq":1,"sampled_at":"2026-09-23T10:00:00Z","temperature":20,"humidity":0.5,"firmware":"1.2.3"}`
	if _, err := ParsePayload([]byte(body)); err != nil {
		t.Fatalf("带扩展字段的合法载荷被拒绝: %v", err)
	}
}
