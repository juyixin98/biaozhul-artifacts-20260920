// Package api 提供机器人任务分配服务的 HTTP 接口(仅标准库 net/http)。
package api

import (
	"encoding/json"
	"errors"
	"net/http"

	"github.com/example/robot-task/internal/model"
	"github.com/example/robot-task/internal/solver"
	"github.com/example/robot-task/internal/validate"
)

// PointDTO 是坐标点的传输对象。
type PointDTO struct {
	X int64 `json:"x"`
	Y int64 `json:"y"`
}

// RobotDTO 是机器人输入。
type RobotDTO struct {
	ID              string   `json:"id"`
	Start           PointDTO `json:"start"`
	StartBattery    int64    `json:"start_battery"`
	BatteryCapacity int64    `json:"battery_capacity"`
	Capacity        int64    `json:"capacity"`
	EnergyPerDist   int64    `json:"energy_per_dist"`
	ChargeRate      int64    `json:"charge_rate"`
}

// TaskDTO 是任务输入。
type TaskDTO struct {
	ID          string   `json:"id"`
	Location    PointDTO `json:"location"`
	Load        int64    `json:"load"`
	Ready       int64    `json:"ready"`
	Due         int64    `json:"due"`
	ServiceTime int64    `json:"service_time,omitempty"`
}

// ChargerDTO 是充电点输入。
type ChargerDTO struct {
	ID       string   `json:"id"`
	Location PointDTO `json:"location"`
}

// PlanRequest 是 POST /api/plan 的请求体。
type PlanRequest struct {
	Robots    []RobotDTO   `json:"robots"`
	Tasks     []TaskDTO    `json:"tasks"`
	Depot     PointDTO     `json:"depot"`
	Chargers  []ChargerDTO `json:"chargers"`
	NodeLimit int64        `json:"node_limit,omitempty"`
}

// PlanResponse 是成功或无解时的响应体。
type PlanResponse struct {
	Feasible      bool              `json:"feasible"`
	Optimal       bool              `json:"optimal"`
	Reason        string            `json:"reason,omitempty"`
	Makespan      int64             `json:"makespan,omitempty"`
	Plans         []model.RobotPlan `json:"plans,omitempty"`
	NodesSearched int64             `json:"nodes_searched"`
	NodeLimit     int64             `json:"node_limit"`
	Validation    *validate.Report  `json:"validation,omitempty"`
}

type errResp struct {
	Error string `json:"error"`
}

// Handler 装配全部路由。
func Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("/api/plan", planHandler)
	return mux
}

func planHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, errResp{Error: "仅支持 POST"})
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)

	var req PlanRequest
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errResp{Error: "请求体不是合法 JSON 或含未知字段: " + err.Error()})
		return
	}
	if err := validateRequest(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errResp{Error: err.Error()})
		return
	}

	prob := toModel(&req)
	res := solver.Solve(prob, req.NodeLimit)

	resp := PlanResponse{
		Feasible:      res.Feasible,
		Optimal:       res.Optimal,
		Reason:        res.Reason,
		Makespan:      res.Makespan,
		Plans:         res.Plans,
		NodesSearched: res.NodesSearched,
		NodeLimit:     res.NodeLimit,
	}
	// 可行解再用独立校验器重放核验，失败则属于服务内部错误(理论上不应发生)。
	if res.Feasible {
		rep := validate.Check(prob, res.Plans)
		resp.Validation = &rep
		if !rep.Valid {
			writeJSON(w, http.StatusInternalServerError, map[string]any{
				"error":      "求解器产出的计划未通过独立校验",
				"validation": rep,
			})
			return
		}
	}
	writeJSON(w, http.StatusOK, resp)
}

func toModel(req *PlanRequest) *model.Problem {
	p := &model.Problem{
		Depot: model.Depot{Location: model.Point{X: req.Depot.X, Y: req.Depot.Y}},
	}
	for _, r := range req.Robots {
		p.Robots = append(p.Robots, model.Robot{
			ID:              r.ID,
			Start:           model.Point{X: r.Start.X, Y: r.Start.Y},
			StartBattery:    r.StartBattery,
			BatteryCapacity: r.BatteryCapacity,
			Capacity:        r.Capacity,
			EnergyPerDist:   r.EnergyPerDist,
			ChargeRate:      r.ChargeRate,
		})
	}
	for _, t := range req.Tasks {
		p.Tasks = append(p.Tasks, model.Task{
			ID:          t.ID,
			Location:    model.Point{X: t.Location.X, Y: t.Location.Y},
			Load:        t.Load,
			Ready:       t.Ready,
			Due:         t.Due,
			ServiceTime: t.ServiceTime,
		})
	}
	for _, c := range req.Chargers {
		p.Chargers = append(p.Chargers, model.Charger{
			ID:       c.ID,
			Location: model.Point{X: c.Location.X, Y: c.Location.Y},
		})
	}
	return p
}

func validateRequest(req *PlanRequest) error {
	if len(req.Robots) == 0 {
		return errors.New("robots 不能为空")
	}
	if len(req.Robots) > 64 {
		return errors.New("robots 数量不能超过 64(离线小实例求解器)")
	}
	if len(req.Tasks) > 64 {
		return errors.New("tasks 数量不能超过 64(离线小实例求解器)")
	}
	if len(req.Tasks) > 0 && len(req.Chargers) == 0 {
		return errors.New("存在任务时 chargers 不能为空(模型要求机器人可规划充电)")
	}
	if req.NodeLimit < 0 {
		return errors.New("node_limit 不能为负")
	}

	ids := map[string]bool{}
	checkID := func(id, kind string) error {
		if id == "" {
			return errors.New(kind + " 存在空 id")
		}
		if ids[kind+":"+id] {
			return errors.New(kind + " id 重复: " + id)
		}
		ids[kind+":"+id] = true
		return nil
	}

	for i := range req.Robots {
		r := &req.Robots[i]
		if err := checkID(r.ID, "robot"); err != nil {
			return err
		}
		if r.BatteryCapacity <= 0 {
			return errors.New("robot " + r.ID + " 的 battery_capacity 必须为正")
		}
		if r.StartBattery < 0 || r.StartBattery > r.BatteryCapacity {
			return errors.New("robot " + r.ID + " 的 start_battery 必须在 [0, battery_capacity]")
		}
		if r.Capacity <= 0 {
			return errors.New("robot " + r.ID + " 的 capacity 必须为正")
		}
		if r.EnergyPerDist < 0 {
			return errors.New("robot " + r.ID + " 的 energy_per_dist 不能为负")
		}
		if r.ChargeRate < 0 {
			return errors.New("robot " + r.ID + " 的 charge_rate 不能为负")
		}
	}
	for i := range req.Tasks {
		t := &req.Tasks[i]
		if err := checkID(t.ID, "task"); err != nil {
			return err
		}
		if t.Load < 0 {
			return errors.New("task " + t.ID + " 的 load 不能为负")
		}
		if t.Ready < 0 || t.Due < 0 {
			return errors.New("task " + t.ID + " 的时间窗不能为负")
		}
		if t.Ready > t.Due {
			return errors.New("task " + t.ID + " 的 ready 晚于 due")
		}
		if t.ServiceTime < 0 {
			return errors.New("task " + t.ID + " 的 service_time 不能为负")
		}
	}
	for i := range req.Chargers {
		c := &req.Chargers[i]
		if err := checkID(c.ID, "charger"); err != nil {
			return err
		}
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}
