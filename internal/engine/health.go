package engine

import (
	"context"
	"database/sql"
	"errors"

	"sensorhealth/internal/model"
)

// Health returns the current health snapshot of one device.
func (e *Engine) Health(ctx context.Context, deviceID string) (*model.Health, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	dev, err := e.st.GetDevice(ctx, deviceID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	cfg, err := e.st.GetDeviceType(ctx, dev.Type)
	if err != nil {
		return nil, err
	}
	s := e.devs[deviceID]
	h := &model.Health{
		DeviceID:       dev.ID,
		DeviceType:     dev.Type,
		Epoch:          dev.Epoch,
		LastSampleTime: dev.LastSample,
		LastHeartbeat:  dev.LastHeartbeat,
		LastRecvAt:     &dev.LastRecvAt,
		ServerTime:     e.clk.Now().UTC(),
		ConfigVersion:  cfg.Version,
		Rules:          map[string]model.RuleHealth{},
	}
	if dev.HasSeq {
		seq := dev.LastSeq
		h.LastSeq = &seq
	}
	for _, name := range []string{model.RuleStale, model.RuleFixed, model.RuleGap} {
		if s == nil {
			h.Rules[name] = model.RuleHealth{State: model.StateOK, Since: dev.LastRecvAt}
			continue
		}
		rs := s.rule(name)
		h.Rules[name] = model.RuleHealth{
			State:   rs.State,
			Since:   rs.Since,
			EventID: rs.EventID,
			Detail:  rs.StaleDetail,
		}
	}
	return h, nil
}

// AllHealth returns health snapshots for every registered device.
func (e *Engine) AllHealth(ctx context.Context) ([]model.Health, error) {
	devs, err := e.st.ListDevices(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]model.Health, 0, len(devs))
	for _, d := range devs {
		h, err := e.Health(ctx, d.ID)
		if err != nil {
			return nil, err
		}
		if h != nil {
			out = append(out, *h)
		}
	}
	return out, nil
}
