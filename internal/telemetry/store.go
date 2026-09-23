package telemetry

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// RawMessage 是一条从 broker 收到的原始 MQTT 消息(每次投递一条,
// 重复投递会产生多条 raw_messages 记录)。
type RawMessage struct {
	Topic    string
	Payload  []byte
	QoS      byte
	Retained bool
	Dup      bool
}

// Outcome 描述一条消息的业务处理结果。
type Outcome string

const (
	OutcomeInserted   Outcome = "inserted"         // 新业务事件,已提交
	OutcomeDuplicate  Outcome = "duplicate"        // 业务键已存在,幂等跳过
	OutcomeStale      Outcome = "stale_generation" // 旧启动代次,落库但不推进设备状态
	OutcomeRetained   Outcome = "retained_replay"  // 保留消息重放,落库但不刷新在线状态
	OutcomeQuarantine Outcome = "quarantined"      // 非法载荷,已入隔离区
)

// Result 是单次处理的返回值。
type Result struct {
	Outcome  Outcome
	SampleID int64 // OutcomeInserted/OutcomeStale/OutcomeRetained 时有效
}

// Storer 是处理存储接口,便于测试注入故障(如提交失败)。
type Storer interface {
	// ProcessValid 在同一事务内持久化:原消息 + 业务采样(去重键)+ 设备状态。
	ProcessValid(ctx context.Context, raw RawMessage, p *Payload) (Result, error)
	// ProcessInvalid 在同一事务内持久化:原消息 + 隔离区记录。
	ProcessInvalid(ctx context.Context, raw RawMessage, reason string) (Result, error)
}

// PGXStore 是基于 PostgreSQL 的 Storer 实现。
type PGXStore struct {
	pool *pgxpool.Pool
}

func NewPGXStore(pool *pgxpool.Pool) *PGXStore { return &PGXStore{pool: pool} }

// ProcessValid 处理一条载荷合法的消息。
//
// 事务内步骤:
//  1. 插入 raw_messages(每次都插,重复投递也留痕);
//  2. 读取设备当前代次,判断是否旧代次(stale);
//  3. 插入 samples,业务键 (device_id, boot_gen, seq) 冲突即重复,幂等跳过;
//  4. 仅当「新事件 且 非保留重放 且 非旧代次」时推进设备在线状态。
//
// 任何一步失败整个事务回滚,调用方不得 ACK,等待 broker 会话重投递。
func (s *PGXStore) ProcessValid(ctx context.Context, raw RawMessage, p *Payload) (Result, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Result{}, fmt.Errorf("开启事务失败: %w", err)
	}
	defer tx.Rollback(ctx) // 提交后调用是 no-op

	rawID, err := insertRaw(ctx, tx, raw)
	if err != nil {
		return Result{}, err
	}

	// 设备当前代次(无记录视为 0)。单消费者串行处理,无需 SELECT FOR UPDATE。
	var currentGen int64
	err = tx.QueryRow(ctx,
		`SELECT current_boot_gen FROM devices WHERE device_id = $1`, *p.DeviceID,
	).Scan(&currentGen)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return Result{}, fmt.Errorf("读取设备状态失败: %w", err)
	}
	stale := *p.BootGen < currentGen

	var sampleID int64
	err = tx.QueryRow(ctx, `
		INSERT INTO samples (device_id, boot_gen, seq, temperature, humidity,
		                     sampled_at, raw_message_id, retained_replay, stale_generation)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
		ON CONFLICT ON CONSTRAINT samples_business_key DO NOTHING
		RETURNING id`,
		*p.DeviceID, *p.BootGen, *p.Seq, *p.Temperature, *p.Humidity,
		p.SampledAt, rawID, raw.Retained, stale,
	).Scan(&sampleID)
	if errors.Is(err, pgx.ErrNoRows) {
		// 业务键冲突:同一 (device_id, boot_gen, seq) 已提交过。
		// 这是 QoS1 至少一次投递的正常产物,幂等跳过即可。
		if err := tx.Commit(ctx); err != nil {
			return Result{}, fmt.Errorf("提交失败(重复分支): %w", err)
		}
		return Result{Outcome: OutcomeDuplicate}, nil
	}
	if err != nil {
		return Result{}, fmt.Errorf("插入采样失败: %w", err)
	}

	outcome := OutcomeInserted
	switch {
	case raw.Retained:
		// 保留消息重放:只留档,绝不能当作新采样刷新在线状态。
		outcome = OutcomeRetained
	case stale:
		// 旧启动代次迟到的消息:留档,不回退设备状态。
		outcome = OutcomeStale
	default:
		if err := upsertDevice(ctx, tx, p); err != nil {
			return Result{}, err
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return Result{}, fmt.Errorf("提交失败: %w", err)
	}
	return Result{Outcome: outcome, SampleID: sampleID}, nil
}

// ProcessInvalid 把载荷非法的消息连同原因写入隔离区。
// 同样是一个事务:原消息 + 隔离区记录一起提交,然后才 ACK,
// 因此不会阻塞其他设备的消息。
func (s *PGXStore) ProcessInvalid(ctx context.Context, raw RawMessage, reason string) (Result, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Result{}, fmt.Errorf("开启事务失败: %w", err)
	}
	defer tx.Rollback(ctx)

	rawID, err := insertRaw(ctx, tx, raw)
	if err != nil {
		return Result{}, err
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO quarantine (raw_message_id, reason) VALUES ($1, $2)`,
		rawID, reason,
	); err != nil {
		return Result{}, fmt.Errorf("写入隔离区失败: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return Result{}, fmt.Errorf("提交失败(隔离区): %w", err)
	}
	return Result{Outcome: OutcomeQuarantine}, nil
}

func insertRaw(ctx context.Context, tx pgx.Tx, raw RawMessage) (int64, error) {
	var id int64
	err := tx.QueryRow(ctx, `
		INSERT INTO raw_messages (topic, payload, qos, retained, dup)
		VALUES ($1,$2,$3,$4,$5) RETURNING id`,
		raw.Topic, raw.Payload, raw.QoS, raw.Retained, raw.Dup,
	).Scan(&id)
	if err != nil {
		return 0, fmt.Errorf("插入原消息失败: %w", err)
	}
	return id, nil
}

// upsertDevice 推进设备状态。代次只允许前进:
//   - 更高代次:接受,序号随之重置为新代次的 seq;
//   - 同代次:seq 取较大者(乱序/重投不会回退);
//   - 更低代次:调用方已拦截,这里 GREATEST 再兜底。
func upsertDevice(ctx context.Context, tx pgx.Tx, p *Payload) error {
	_, err := tx.Exec(ctx, `
		INSERT INTO devices (device_id, current_boot_gen, last_seq, last_sample_at, updated_at)
		VALUES ($1, $2, $3, $4, now())
		ON CONFLICT (device_id) DO UPDATE SET
			current_boot_gen = GREATEST(devices.current_boot_gen, EXCLUDED.current_boot_gen),
			last_seq = CASE
				WHEN EXCLUDED.current_boot_gen > devices.current_boot_gen THEN EXCLUDED.last_seq
				ELSE GREATEST(devices.last_seq, EXCLUDED.last_seq)
			END,
			last_sample_at = GREATEST(devices.last_sample_at, EXCLUDED.last_sample_at),
			updated_at = now()`,
		*p.DeviceID, *p.BootGen, *p.Seq, p.SampledAt,
	)
	if err != nil {
		return fmt.Errorf("更新设备状态失败: %w", err)
	}
	return nil
}
