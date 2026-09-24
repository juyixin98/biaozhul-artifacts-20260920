// Package model defines the input/output data model for the offline
// multi-robot task allocation problem and validates requests.
package model

import (
	"fmt"
	"math"
)

// Point is a point on the 2-D integer grid.
type Point struct {
	X int64 `json:"x"`
	Y int64 `json:"y"`
}

// Robot is an available robot.
type Robot struct {
	ID              string `json:"id"`
	Start           Point  `json:"start"`
	Battery         int64  `json:"battery"`         // initial energy level
	BatteryCapacity int64  `json:"batteryCapacity"` // maximum energy level
	ChargeRate      int64  `json:"chargeRate"`      // energy gained per time unit at a charging point
	PayloadCapacity int64  `json:"payloadCapacity"` // maximum task payload the robot can carry
}

// Task is a delivery/service task with a time window [Ready, Due].
type Task struct {
	ID      string `json:"id"`
	Loc     Point  `json:"loc"`
	Payload int64  `json:"payload"`
	Ready   int64  `json:"ready"`   // earliest service start time
	Due     int64  `json:"due"`     // latest service start time
	Service int64  `json:"service"` // service duration, integer time units
}

// Charger is a charging point. Charging always restores the battery to full.
type Charger struct {
	ID string `json:"id"`
	// Loc is the charger location. Optional: when both x and y are 0 the
	// charger is assumed to sit at the origin.
	Loc Point `json:"loc"`
}

// Request is the body of POST /api/v1/plans.
type Request struct {
	// EnergyPerDist is the energy consumed per unit of Manhattan travel
	// distance. Zero defaults to 1.
	EnergyPerDist int64 `json:"energyPerDist,omitempty"`

	Robots   []Robot   `json:"robots"`
	Tasks    []Task    `json:"tasks"`
	Chargers []Charger `json:"chargers"`
}

// Step is one entry in a robot's route: either a task service or a
// charging stop. Movement between consecutive locations is implied;
// the fields make every quantity explicit so the plan is checkable.
type Step struct {
	Type string `json:"type"` // "task" | "charge"

	TaskID    string `json:"taskID,omitempty"`
	ChargerID string `json:"chargerID,omitempty"`

	From Point `json:"from"`
	To   Point `json:"to"`

	MoveDist int64 `json:"moveDist"`
	MoveTime int64 `json:"moveTime"`
	MoveCost int64 `json:"moveCost"` // energy spent moving to this stop

	ArriveTime    int64 `json:"arriveTime"`
	StartTime     int64 `json:"startTime"` // after any waiting (tasks only)
	ChargeTime    int64 `json:"chargeTime,omitempty"`
	EndTime       int64 `json:"endTime"`
	BatteryBefore int64 `json:"batteryBefore"` // energy level right upon arrival
	BatteryAfter  int64 `json:"batteryAfter"`  // energy level at end of this step
}

// RobotPlan is the assigned route of one robot (possibly empty).
type RobotPlan struct {
	RobotID  string `json:"robotID"`
	Start    Point  `json:"start"`
	Steps    []Step `json:"steps"`
	FinishAt int64  `json:"finishAt"` // end time of the last step, 0 when idle
}

// Response is the solver result.
type Response struct {
	Feasible bool        `json:"feasible"`
	Reason   string      `json:"reason,omitempty"`
	Makespan int64       `json:"makespan,omitempty"`
	Plans    []RobotPlan `json:"plans,omitempty"`
}

// Hard size limits keep exhaustive search practical.
const (
	MaxRobots   = 8
	MaxTasks    = 10
	MaxChargers = 6
)

// Validate checks structural consistency of the request and returns the
// normalized energy-per-distance value.
func (r *Request) Validate() (int64, error) {
	if len(r.Robots) == 0 {
		return 0, fmt.Errorf("at least one robot is required")
	}
	if len(r.Robots) > MaxRobots {
		return 0, fmt.Errorf("too many robots: %d > %d", len(r.Robots), MaxRobots)
	}
	if len(r.Tasks) > MaxTasks {
		return 0, fmt.Errorf("too many tasks: %d > %d", len(r.Tasks), MaxTasks)
	}
	if len(r.Chargers) > MaxChargers {
		return 0, fmt.Errorf("too many chargers: %d > %d", len(r.Chargers), MaxChargers)
	}

	ids := map[string]bool{}
	for i, rb := range r.Robots {
		if rb.ID == "" {
			return 0, fmt.Errorf("robot[%d]: id is required", i)
		}
		if ids[rb.ID] {
			return 0, fmt.Errorf("duplicate robot id %q", rb.ID)
		}
		ids[rb.ID] = true
		if rb.BatteryCapacity <= 0 {
			return 0, fmt.Errorf("robot %q: batteryCapacity must be positive", rb.ID)
		}
		if rb.Battery < 0 || rb.Battery > rb.BatteryCapacity {
			return 0, fmt.Errorf("robot %q: battery must be in [0, batteryCapacity]", rb.ID)
		}
		if rb.ChargeRate <= 0 {
			return 0, fmt.Errorf("robot %q: chargeRate must be positive", rb.ID)
		}
		if rb.PayloadCapacity < 0 {
			return 0, fmt.Errorf("robot %q: payloadCapacity must be non-negative", rb.ID)
		}
	}

	taskIDs := map[string]bool{}
	for i, t := range r.Tasks {
		if t.ID == "" {
			return 0, fmt.Errorf("task[%d]: id is required", i)
		}
		if taskIDs[t.ID] {
			return 0, fmt.Errorf("duplicate task id %q", t.ID)
		}
		taskIDs[t.ID] = true
		if t.Payload < 0 {
			return 0, fmt.Errorf("task %q: payload must be non-negative", t.ID)
		}
		if t.Ready < 0 {
			return 0, fmt.Errorf("task %q: ready must be non-negative", t.ID)
		}
		if t.Due < t.Ready {
			return 0, fmt.Errorf("task %q: due (%d) is before ready (%d)", t.ID, t.Due, t.Ready)
		}
		if t.Service < 0 {
			return 0, fmt.Errorf("task %q: service duration must be non-negative", t.ID)
		}
	}

	chargerIDs := map[string]bool{}
	for i, c := range r.Chargers {
		if c.ID == "" {
			return 0, fmt.Errorf("charger[%d]: id is required", i)
		}
		if chargerIDs[c.ID] {
			return 0, fmt.Errorf("duplicate charger id %q", c.ID)
		}
		chargerIDs[c.ID] = true
	}

	epd := r.EnergyPerDist
	if epd == 0 {
		epd = 1
	}
	if epd < 0 {
		return 0, fmt.Errorf("energyPerDist must be non-negative")
	}
	return epd, nil
}

// EnergyPerDistOr1 returns the configured energy per unit distance,
// defaulting to 1 when unset (Validate performs the same defaulting).
func (r *Request) EnergyPerDistOr1() int64 {
	if r.EnergyPerDist <= 0 {
		return 1
	}
	return r.EnergyPerDist
}

// Dist returns the Manhattan distance between two points. Travel time is
// defined to equal distance (unit speed on the integer grid).
func Dist(a, b Point) int64 {
	d := int64(math.Abs(float64(a.X-b.X))) + int64(math.Abs(float64(a.Y-b.Y)))
	return d
}
