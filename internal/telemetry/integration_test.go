//go:build integration

// 集成测试:需要本地 Mosquitto (TEST_MQTT_BROKER) 与 PostgreSQL (TEST_DATABASE_URL)。
// 运行方式见 README.md 与 scripts/run_tests.sh。
package telemetry

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"sync/atomic"
	"testing"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	testBroker string
	testDB     string
	pool       *pgxpool.Pool
	pub        mqtt.Client
	clientSeq  atomic.Int64
)

func TestMain(m *testing.M) {
	testBroker = getenv("TEST_MQTT_BROKER", "tcp://127.0.0.1:1883")
	testDB = getenv("TEST_DATABASE_URL", "postgres://mqtt:mqtt_secret@127.0.0.1:5432/mqtt_telemetry_test")

	ctx := context.Background()
	cfg, err := pgxpool.ParseConfig(testDB)
	if err != nil {
		log.Fatalf("解析 DATABASE_URL: %v", err)
	}
	// 简单协议,允许一次执行多条 DDL(schema.sql 含多条语句)
	cfg.ConnConfig.DefaultQueryExecMode = pgx.QueryExecModeSimpleProtocol
	pool, err = pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		log.Fatalf("连接测试库失败: %v", err)
	}
	if _, err := pool.Exec(ctx,
		`DROP TABLE IF EXISTS quarantine, samples, devices, raw_messages CASCADE`); err != nil {
		log.Fatalf("清理测试库失败: %v", err)
	}
	schema, err := os.ReadFile("../../db/schema.sql")
	if err != nil {
		log.Fatalf("读取 schema: %v", err)
	}
	if _, err := pool.Exec(ctx, string(schema)); err != nil {
		log.Fatalf("建表失败: %v", err)
	}

	opts := mqtt.NewClientOptions().
		AddBroker(testBroker).
		SetClientID(fmt.Sprintf("itest-pub-%d", time.Now().UnixNano())).
		SetCleanSession(true)
	pub = mqtt.NewClient(opts)
	if t := pub.Connect(); t.WaitTimeout(5*time.Second) && t.Error() != nil {
		log.Fatalf("发布端连接 broker 失败: %v", t.Error())
	}

	code := m.Run()
	pub.Disconnect(250)
	pool.Close()
	os.Exit(code)
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// ---- 测试工具 ----

func payloadJSON(dev string, gen, seq int64, temp float64) string {
	return fmt.Sprintf(`{"device_id":%q,"boot_gen":%d,"seq":%d,`+
		`"sampled_at":"2026-09-23T10:00:00Z","temperature":%v,"humidity":0.5}`,
		dev, gen, seq, temp)
}

func topicFor(dev string) string { return "devices/" + dev + "/telemetry" }

func publish(t *testing.T, topic, payload string, retained bool) {
	t.Helper()
	tok := pub.Publish(topic, 1, retained, payload)
	if !tok.WaitTimeout(5*time.Second) || tok.Error() != nil {
		t.Fatalf("发布失败: %v", tok.Error())
	}
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("等待超时: %s", what)
}

func queryInt(t *testing.T, sql string, args ...any) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(context.Background(), sql, args...).Scan(&n); err != nil {
		t.Fatalf("查询失败: %v", err)
	}
	return n
}

// newIngest 启动一个 ingest 客户端(持久会话 + 手动 ACK),返回前确保订阅生效。
func newIngest(t *testing.T, store Storer, topic string) mqtt.Client {
	t.Helper()
	h := NewHandler(store, log.New(io.Discard, "", 0))
	cid := fmt.Sprintf("itest-sub-%d-%d", time.Now().UnixNano(), clientSeq.Add(1))
	subscribed := make(chan struct{}, 1)

	opts := mqtt.NewClientOptions().
		AddBroker(testBroker).
		SetClientID(cid).
		SetCleanSession(false). // 持久会话:重连后重投递未确认消息
		SetAutoAckDisabled(true).
		SetOrderMatters(true).
		SetAutoReconnect(true).
		SetOnConnectHandler(func(c mqtt.Client) {
			tok := c.Subscribe(topic, 1, h.Handle)
			tok.Wait()
			select {
			case subscribed <- struct{}{}:
			default:
			}
		})
	c := mqtt.NewClient(opts)
	h.SetReconnect(func() {
		c.Disconnect(250)
		go func() { c.Connect() }()
	})
	if tok := c.Connect(); tok.WaitTimeout(5*time.Second) && tok.Error() != nil {
		t.Fatalf("ingest 连接失败: %v", tok.Error())
	}
	select {
	case <-subscribed:
	case <-time.After(5 * time.Second):
		t.Fatalf("订阅超时")
	}
	t.Cleanup(func() { c.Disconnect(250) })
	return c
}

// ---- 用例 1:QoS1 重复投递(传输至少一次 ≠ 业务重复) ----

func TestDuplicateDelivery(t *testing.T) {
	dev := "dup-1"
	newIngest(t, NewPGXStore(pool), "devices/+/telemetry")

	body := payloadJSON(dev, 1, 1, 21.0)
	publish(t, topicFor(dev), body, false)
	publish(t, topicFor(dev), body, false) // 同一业务事件再次投递

	waitFor(t, "两次投递都被接收", func() bool {
		return queryInt(t,
			`SELECT count(*) FROM raw_messages WHERE topic=$1`, topicFor(dev)) == 2
	})

	if n := queryInt(t, `SELECT count(*) FROM samples WHERE device_id=$1`, dev); n != 1 {
		t.Fatalf("业务去重失败: samples=%d, 期望 1", n)
	}
	if n := queryInt(t, `SELECT last_seq FROM devices WHERE device_id=$1`, dev); n != 1 {
		t.Fatalf("设备状态错误: last_seq=%d, 期望 1", n)
	}
}

// ---- 用例 2:设备重启(代次隔离序号)+ 旧代次迟到消息 ----

func TestDeviceReboot(t *testing.T) {
	dev := "boot-1"
	newIngest(t, NewPGXStore(pool), "devices/+/telemetry")

	for seq := int64(1); seq <= 3; seq++ { // 第 1 代:seq 1..3
		publish(t, topicFor(dev), payloadJSON(dev, 1, seq, 20), false)
	}
	for seq := int64(1); seq <= 2; seq++ { // 重启,第 2 代:seq 重新从 1 计
		publish(t, topicFor(dev), payloadJSON(dev, 2, seq, 22), false)
	}

	waitFor(t, "两代共 5 条采样落库", func() bool {
		return queryInt(t, `SELECT count(*) FROM samples WHERE device_id=$1`, dev) == 5
	})

	var gen, seq int
	if err := pool.QueryRow(context.Background(),
		`SELECT current_boot_gen, last_seq FROM devices WHERE device_id=$1`, dev,
	).Scan(&gen, &seq); err != nil {
		t.Fatal(err)
	}
	if gen != 2 || seq != 2 {
		t.Fatalf("重启后设备状态错误: gen=%d seq=%d, 期望 gen=2 seq=2", gen, seq)
	}

	// 旧代次迟到的消息:落库留痕,但不得回退设备状态
	publish(t, topicFor(dev), payloadJSON(dev, 1, 4, 19), false)
	waitFor(t, "旧代次消息落库", func() bool {
		return queryInt(t,
			`SELECT count(*) FROM samples WHERE device_id=$1 AND stale_generation`, dev) == 1
	})
	if err := pool.QueryRow(context.Background(),
		`SELECT current_boot_gen, last_seq FROM devices WHERE device_id=$1`, dev,
	).Scan(&gen, &seq); err != nil {
		t.Fatal(err)
	}
	if gen != 2 || seq != 2 {
		t.Fatalf("旧代次消息污染了设备状态: gen=%d seq=%d", gen, seq)
	}
}

// ---- 用例 3:保留消息不得刷新在线状态 ----

func TestRetainedMessage(t *testing.T) {
	dev := "ret-1"
	topic := "retained-test/+/telemetry"

	// 1. 没有任何订阅者时发布保留消息
	publish(t, "retained-test/"+dev+"/telemetry", payloadJSON(dev, 1, 1, 20), true)
	time.Sleep(300 * time.Millisecond) // 确保 broker 已存好

	// 2. 新订阅者收到的是 RETAIN=1 的重放:落库但不建在线状态
	newIngest(t, NewPGXStore(pool), topic)
	waitFor(t, "保留重放落库", func() bool {
		return queryInt(t,
			`SELECT count(*) FROM samples WHERE device_id=$1 AND retained_replay`, dev) == 1
	})
	if n := queryInt(t, `SELECT count(*) FROM devices WHERE device_id=$1`, dev); n != 0 {
		t.Fatalf("保留消息被当成新采样刷新了在线状态")
	}

	// 3. 设备真正上线:发一条普通消息
	publish(t, "retained-test/"+dev+"/telemetry", payloadJSON(dev, 1, 2, 21), false)
	waitFor(t, "设备上线", func() bool {
		return queryInt(t, `SELECT count(*) FROM devices WHERE device_id=$1`, dev) == 1
	})
	var lastSampleAt time.Time
	if err := pool.QueryRow(context.Background(),
		`SELECT last_sample_at FROM devices WHERE device_id=$1`, dev,
	).Scan(&lastSampleAt); err != nil {
		t.Fatal(err)
	}

	// 4. 又一个订阅者出现,再次收到保留重放(业务上是重复):
	//    在线状态一个字节都不许动
	newIngest(t, NewPGXStore(pool), topic)
	waitFor(t, "第二次保留重放被接收", func() bool {
		return queryInt(t,
			`SELECT count(*) FROM raw_messages WHERE topic=$1 AND retained`,
			"retained-test/"+dev+"/telemetry") == 2
	})
	var got time.Time
	if err := pool.QueryRow(context.Background(),
		`SELECT last_sample_at FROM devices WHERE device_id=$1`, dev,
	).Scan(&got); err != nil {
		t.Fatal(err)
	}
	if !got.Equal(lastSampleAt) {
		t.Fatalf("保留重放刷新了在线状态: %v -> %v", lastSampleAt, got)
	}
	if n := queryInt(t, `SELECT count(*) FROM samples WHERE device_id=$1`, dev); n != 2 {
		t.Fatalf("samples=%d, 期望 2(保留重放不产生新业务事件)", n)
	}
}

// ---- 用例 4:业务提交失败 → 不 ACK → 会话重投递 → 只提交一次 ----

type flakyStore struct {
	inner         Storer
	failRemaining atomic.Int32
	attempts      atomic.Int32
	dupSeen       atomic.Bool
}

func (f *flakyStore) ProcessValid(ctx context.Context, raw RawMessage, p *Payload) (Result, error) {
	f.attempts.Add(1)
	if raw.Dup {
		f.dupSeen.Store(true)
	}
	if f.failRemaining.Load() > 0 {
		f.failRemaining.Add(-1)
		return Result{}, errors.New("注入的业务提交失败")
	}
	return f.inner.ProcessValid(ctx, raw, p)
}

func (f *flakyStore) ProcessInvalid(ctx context.Context, raw RawMessage, reason string) (Result, error) {
	return f.inner.ProcessInvalid(ctx, raw, reason)
}

func TestCommitFailureTriggersSessionRedelivery(t *testing.T) {
	dev := "fail-1"
	fs := &flakyStore{inner: NewPGXStore(pool)}
	fs.failRemaining.Store(2) // 前两次提交都失败

	newIngest(t, fs, "devices/+/telemetry")
	publish(t, topicFor(dev), payloadJSON(dev, 1, 1, 23), false)

	waitFor(t, "重投递后提交成功", func() bool {
		return queryInt(t, `SELECT count(*) FROM samples WHERE device_id=$1`, dev) == 1
	})
	time.Sleep(500 * time.Millisecond) // 确认没有更多重投

	if got := fs.attempts.Load(); got < 3 {
		t.Fatalf("处理次数=%d, 期望 >=3(2 次失败 + 1 次成功)", got)
	}
	if !fs.dupSeen.Load() {
		t.Fatalf("未观察到 DUP=1 的协议级重投递")
	}
	if n := queryInt(t, `SELECT count(*) FROM samples WHERE device_id=$1`, dev); n != 1 {
		t.Fatalf("samples=%d, 期望 1(多次投递只提交一次)", n)
	}
	// 失败的事务整体回滚,原消息也不应残留
	if n := queryInt(t, `SELECT count(*) FROM raw_messages WHERE topic=$1`, topicFor(dev)); n != 1 {
		t.Fatalf("raw_messages=%d, 期望 1(失败事务已回滚)", n)
	}
}

// ---- 用例 5:非法载荷入隔离区,不阻塞其他设备 ----

func TestQuarantineDoesNotBlockOthers(t *testing.T) {
	newIngest(t, NewPGXStore(pool), "devices/+/telemetry")

	publish(t, topicFor("bad-1"), `{"device_id":"bad-1","boot_gen":1,"seq":1,"temperature":"hot"}`, false)
	publish(t, topicFor("bad-1"), `garbage-not-json`, false)
	publish(t, topicFor("good-1"), payloadJSON("good-1", 1, 1, 18.5), false)

	waitFor(t, "两条非法消息入隔离区", func() bool {
		return queryInt(t, `SELECT count(*) FROM quarantine q
			JOIN raw_messages r ON r.id = q.raw_message_id
			WHERE r.topic=$1`, topicFor("bad-1")) == 2
	})
	waitFor(t, "正常设备被处理", func() bool {
		return queryInt(t, `SELECT count(*) FROM samples WHERE device_id='good-1'`) == 1
	})
	if n := queryInt(t, `SELECT count(*) FROM samples WHERE device_id='bad-1'`); n != 0 {
		t.Fatalf("非法载荷产生了业务数据")
	}
}
