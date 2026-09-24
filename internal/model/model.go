// Package model 定义离线多机器人任务分配问题的领域模型。
//
// 全部时间、距离、电量、载荷均为整数(int64)：
//   - 距离采用曼哈顿距离，因此旅行耗时与旅行距离在数值上相等；
//   - 旅行耗能 = 距离 * EnergyPerDist；
//   - 任务为“在 depot 取货，在自身时间窗内送达”的配送任务。
package model

// Point 是二维整数坐标点。
type Point struct {
	X int64 `json:"x"`
	Y int64 `json:"y"`
}

// Depot 是取货点，所有任务的货物都在这里。
type Depot struct {
	Location Point `json:"location"`
}

// Charger 是充电点，机器人到达后可把电池充至满电。
type Charger struct {
	ID       string `json:"id"`
	Location Point  `json:"location"`
}

// Task 是一个配送任务：在 Depot 取货(载荷 Load)，
// 必须在整数时间窗 [Ready, Due] 内开始在 Location 的卸货服务，
// 卸货耗时 ServiceTime。
type Task struct {
	ID          string `json:"id"`
	Location    Point  `json:"location"`
	Load        int64  `json:"load"`
	Ready       int64  `json:"ready"`
	Due         int64  `json:"due"`
	ServiceTime int64  `json:"service_time"`
}

// Robot 是一台机器人。
//   - Start: 初始位置；StartBattery: 初始电量(<= BatteryCapacity)
//   - BatteryCapacity: 电池容量上限
//   - Capacity: 可同时承载的最大载荷
//   - EnergyPerDist: 每移动一个曼哈顿单位消耗的电量
//   - ChargeRate: 在充电点每补充 1 单位电量需要的时间
type Robot struct {
	ID              string `json:"id"`
	Start           Point  `json:"start"`
	StartBattery    int64  `json:"start_battery"`
	BatteryCapacity int64  `json:"battery_capacity"`
	Capacity        int64  `json:"capacity"`
	EnergyPerDist   int64  `json:"energy_per_dist"`
	ChargeRate      int64  `json:"charge_rate"`
}

// Problem 是一次离线规划的完整输入。
type Problem struct {
	Robots   []Robot   `json:"robots"`
	Tasks    []Task    `json:"tasks"`
	Depot    Depot     `json:"depot"`
	Chargers []Charger `json:"chargers"`
}

// Action 是计划中的一个动作段。
// 类型：
//   - "travel": 从 From 行驶到 To(To 可能是普通途经点、depot 或任务点)；
//   - "charge": 从 From 行驶到某充电点并充电，ServiceTime 为充电耗时；
//   - "pickup": 在 depot 的零长度取货事件(From==To==depot)；
//   - "service": 在任务点的零长度服务事件(From==To==任务点)，
//     若 ArrivalTime 早于 Ready，等待体现在 EndTime-ArrivalTime-ServiceTime 中。
type Action struct {
	RobotID       string `json:"robot_id"`
	Type          string `json:"type"` // "travel" | "charge" | "pickup" | "service"
	TaskID        string `json:"task_id,omitempty"`
	ChargerID     string `json:"charger_id,omitempty"`
	From          Point  `json:"from"`
	To            Point  `json:"to"`
	StartTime     int64  `json:"start_time"`
	TravelTime    int64  `json:"travel_time"`
	ArrivalTime   int64  `json:"arrival_time"`
	ServiceTime   int64  `json:"service_time"` // 服务或充电耗时(不含等待)
	EndTime       int64  `json:"end_time"`     // 含可能的等待
	BatteryBefore int64  `json:"battery_before"`
	BatteryAfter  int64  `json:"battery_after"`
}

// RobotPlan 是单台机器人的完整计划。
type RobotPlan struct {
	RobotID  string   `json:"robot_id"`
	Actions  []Action `json:"actions"`
	Makespan int64    `json:"makespan"` // 最后一个动作结束时间，无任务则为 0
}

// Result 是规划结果。Feasible=false 时 Plans 为 nil。
type Result struct {
	Feasible      bool        `json:"feasible"`
	Optimal       bool        `json:"optimal"`
	Reason        string      `json:"reason,omitempty"`
	Makespan      int64       `json:"makespan,omitempty"`
	Plans         []RobotPlan `json:"plans,omitempty"`
	NodesSearched int64       `json:"nodes_searched"`
	NodeLimit     int64       `json:"node_limit"`
}

// Dist 返回曼哈顿距离（整数）。
func Dist(a, b Point) int64 {
	dx := a.X - b.X
	if dx < 0 {
		dx = -dx
	}
	dy := a.Y - b.Y
	if dy < 0 {
		dy = -dy
	}
	return dx + dy
}
