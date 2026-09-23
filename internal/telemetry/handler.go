package telemetry

import (
	"context"
	"log"
	"sync"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
)

// Handler 把 MQTT 消息接到事务存储上。
//
// 核心约定:先提交,后 ACK。
//   - 处理成功(含重复、旧代次、保留重放、隔离区)→ msg.Ack();
//   - 事务提交失败 → 不 ACK,触发重连;MQTT 3.1.1 没有 NACK,
//     未确认的 QoS1 消息只能靠「持久会话 + 重连」由 broker 以 DUP=1 重投递,
//     这正是「会话重投递」。
type Handler struct {
	store  Storer
	logger *log.Logger

	mu         sync.Mutex
	reconnect  func() // 由接入层注入,触发客户端重连
	reconnOnce map[string]struct{}
}

func NewHandler(store Storer, logger *log.Logger) *Handler {
	return &Handler{
		store:      store,
		logger:     logger,
		reconnOnce: make(map[string]struct{}),
	}
}

// SetReconnect 注入重连函数(提交失败后调用)。
func (h *Handler) SetReconnect(f func()) { h.reconnect = f }

// Handle 实现 mqtt.MessageHandler。
func (h *Handler) Handle(_ mqtt.Client, msg mqtt.Message) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	raw := RawMessage{
		Topic:    msg.Topic(),
		Payload:  append([]byte(nil), msg.Payload()...),
		QoS:      msg.Qos(),
		Retained: msg.Retained(),
		Dup:      msg.Duplicate(),
	}

	p, perr := ParsePayload(raw.Payload)

	var (
		res Result
		err error
	)
	if perr != nil {
		res, err = h.store.ProcessInvalid(ctx, raw, perr.Error())
	} else {
		res, err = h.store.ProcessValid(ctx, raw, p)
	}

	if err != nil {
		// 业务提交失败:不 ACK。消息留在 broker 的会话里,
		// 重连后会被以 DUP=1 重新投递,直到提交成功。
		h.logger.Printf("提交失败,不 ACK,等待会话重投递: topic=%s err=%v", raw.Topic, err)
		h.scheduleReconnect()
		return
	}

	msg.Ack() // 提交成功才确认
	h.logger.Printf("已处理: topic=%s outcome=%s retained=%v dup=%v",
		raw.Topic, res.Outcome, raw.Retained, raw.Dup)
}

// scheduleReconnect 防抖触发重连:同一轮故障只重连一次,
// 避免连续失败的消息引发重连风暴。
func (h *Handler) scheduleReconnect() {
	if h.reconnect == nil {
		return
	}
	h.mu.Lock()
	if _, ok := h.reconnOnce["pending"]; ok {
		h.mu.Unlock()
		return
	}
	h.reconnOnce["pending"] = struct{}{}
	h.mu.Unlock()

	go func() {
		time.Sleep(500 * time.Millisecond)
		h.mu.Lock()
		delete(h.reconnOnce, "pending")
		h.mu.Unlock()
		h.reconnect()
	}()
}
