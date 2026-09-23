// 遥测接收后端:订阅 MQTT,QoS1 至少一次投递 + 业务键去重,
// 原消息/业务状态/去重键同一事务提交,提交成功才 ACK。
package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
	"github.com/jackc/pgx/v5/pgxpool"

	"mqtt-redelivery/internal/telemetry"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	logger := log.New(os.Stdout, "[ingest] ", log.LstdFlags|log.Lmicroseconds)

	var (
		broker   = env("MQTT_BROKER", "tcp://127.0.0.1:1883")
		clientID = env("MQTT_CLIENT_ID", "telemetry-ingest")
		topic    = env("MQTT_TOPIC", "devices/+/telemetry")
		dbURL    = env("DATABASE_URL", "postgres://mqtt:mqtt_secret@127.0.0.1:5432/mqtt_telemetry")
	)

	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dbURL)
	if err != nil {
		logger.Fatalf("连接池初始化失败: %v", err)
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		logger.Fatalf("数据库不可达: %v", err)
	}
	logger.Printf("数据库已连接: %s", dbURL)

	handler := telemetry.NewHandler(telemetry.NewPGXStore(pool), logger)

	opts := mqtt.NewClientOptions().
		AddBroker(broker).
		SetClientID(clientID).
		// 持久会话:重连后 broker 会重投递本会话未确认的 QoS1 消息
		SetCleanSession(false).
		// 关闭自动 ACK,改为「事务提交成功后」手动 ACK
		SetAutoAckDisabled(true).
		SetOrderMatters(true).
		SetAutoReconnect(true).
		SetConnectRetry(true).
		SetOnConnectHandler(func(c mqtt.Client) {
			t := c.Subscribe(topic, 1, handler.Handle)
			if t.WaitTimeout(10*time.Second) && t.Error() != nil {
				logger.Printf("订阅失败: %v", t.Error())
				return
			}
			logger.Printf("已订阅 %s (QoS1)", topic)
		}).
		SetConnectionLostHandler(func(_ mqtt.Client, err error) {
			logger.Printf("连接断开: %v(等待自动重连)", err)
		})

	client := mqtt.NewClient(opts)

	// 提交失败后的补救:MQTT 3.1.1 没有 NACK,未确认的 QoS1 消息
	// 只能靠持久会话 + 重连让 broker 以 DUP=1 重投递。
	handler.SetReconnect(func() {
		logger.Printf("强制重连,触发 broker 会话重投递")
		client.Disconnect(250)
		go func() {
			for {
				t := client.Connect()
				if t.WaitTimeout(5*time.Second) && t.Error() == nil {
					return
				}
				time.Sleep(time.Second)
			}
		}()
	})

	if t := client.Connect(); t.WaitTimeout(10*time.Second) && t.Error() != nil {
		logger.Fatalf("连接 broker 失败: %v", t.Error())
	}
	logger.Printf("broker 已连接: %s clientID=%s cleanSession=false", broker, clientID)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	logger.Printf("退出中…")
	client.Disconnect(500)
}
