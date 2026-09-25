// Package server 提供调度库的本地 HTTP 接口。
//
// 时间在 HTTP 边界上使用 RFC3339（UTC），内部统一映射为
// “自 Unix 纪元起的分钟数”这一整数 tick（半开区间语义）。
// 时钟与执行器均可注入：测试中使用 clock.Fake 与可编排的
// executor，生产中使用墙钟与 Noop/业务执行器。
package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"resourcebooking/internal/clock"
	"resourcebooking/internal/dispatcher"
	"resourcebooking/internal/executor"
	"resourcebooking/internal/scheduler"
)

// Config 构造 Server 所需配置。
type Config struct {
	Clock    clock.Clock       // nil = 墙钟
	Executor executor.Executor // nil = Noop
	// Epoch 为 tick=0 对应的墙钟时刻；零值 = Unix 纪元。
	Epoch time.Time
	// DispatchEvery 为后台推进间隔；零值 = 1 分钟。
	DispatchEvery time.Duration
}

// Server 持有调度器、分发器与时间换算。
type Server struct {
	sch   *scheduler.Scheduler
	clk   clock.Clock
	epoch time.Time
	disp  *dispatcher.Runner
}

// New 创建 Server。
func New(sch *scheduler.Scheduler, cfg Config) *Server {
	if cfg.Clock == nil {
		cfg.Clock = clock.Wall{}
	}
	if cfg.Epoch.IsZero() {
		cfg.Epoch = time.Unix(0, 0).UTC()
	}
	if cfg.DispatchEvery <= 0 {
		cfg.DispatchEvery = time.Minute
	}
	s := &Server{
		sch:   sch,
		clk:   cfg.Clock,
		epoch: cfg.Epoch.UTC(),
	}
	s.disp = dispatcher.NewRunner(sch, cfg.Clock, dispatcher.Minute, cfg.DispatchEvery, cfg.Executor,
		dispatcher.WithEpoch(cfg.Epoch))
	return s
}

// Dispatcher 暴露分发器，供 main 启动后台循环。
func (s *Server) Dispatcher() *dispatcher.Runner { return s.disp }

// Scheduler 暴露底层调度器（供测试/种子装载直接调用）。
func (s *Server) Scheduler() *scheduler.Scheduler { return s.sch }

// ToTick 把墙钟时间映射为分钟 tick。
// 非整分钟对齐的时间戳返回错误（我们只承诺分钟粒度）。
func (s *Server) ToTick(t time.Time) (scheduler.Ticks, error) {
	return s.toTick(t)
}

// toTick 把墙钟时间映射为分钟 tick。
// 非整分钟对齐的时间戳返回错误（我们只承诺分钟粒度）。
func (s *Server) toTick(t time.Time) (scheduler.Ticks, error) {
	t = t.UTC()
	if t.Second() != 0 || t.Nanosecond() != 0 {
		return 0, fmt.Errorf("时间 %s 未对齐到分钟（秒/纳秒必须为 0）", t.Format(time.RFC3339))
	}
	return scheduler.Ticks(t.Sub(s.epoch) / time.Minute), nil
}

// toTime 把分钟 tick 映射回墙钟时间。
func (s *Server) toTime(t scheduler.Ticks) time.Time {
	return s.epoch.Add(time.Duration(t) * time.Minute)
}

// intervalDTO 是半开区间的 JSON 表示。
type intervalDTO struct {
	Start time.Time `json:"start"`
	End   time.Time `json:"end"`
}

func (s *Server) parseInterval(d intervalDTO) (scheduler.Interval, error) {
	a, err := s.toTick(d.Start)
	if err != nil {
		return scheduler.Interval{}, invalidInput(err.Error())
	}
	b, err := s.toTick(d.End)
	if err != nil {
		return scheduler.Interval{}, invalidInput(err.Error())
	}
	iv := scheduler.Interval{Start: a, End: b}
	if err := iv.Validate(); err != nil {
		return scheduler.Interval{}, invalidInput(err.Error())
	}
	return iv, nil
}

// invalidInput 把 HTTP 边界的输入问题（时间格式、区间非法等）
// 统一包装成结构化 invalid_request，使响应状态码为 400。
func invalidInput(msg string) error {
	return &scheduler.Error{Code: scheduler.CodeInvalidRequest, Message: msg}
}

func (s *Server) intervalDTO(iv scheduler.Interval) intervalDTO {
	return intervalDTO{Start: s.toTime(iv.Start), End: s.toTime(iv.End)}
}

// ---------- 请求/响应 DTO ----------

type resourceReq struct {
	ID       string  `json:"id"`
	Name     string  `json:"name,omitempty"`
	Capacity []int64 `json:"capacity"`
}

type reservationReq struct {
	ID       string            `json:"id,omitempty"`
	Resource string            `json:"resource"`
	Interval intervalDTO       `json:"interval"`
	Demand   scheduler.Demand  `json:"demand"`
	Meta     map[string]string `json:"meta,omitempty"`
}

type placeItemReq struct {
	ID              string            `json:"id,omitempty"`
	Resource        string            `json:"resource"`
	Window          intervalDTO       `json:"window"`
	DurationMinutes int64             `json:"duration_minutes"`
	Earliest        *time.Time        `json:"earliest,omitempty"`
	Demand          scheduler.Demand  `json:"demand"`
	Meta            map[string]string `json:"meta,omitempty"`
}

type fixedItemReq struct {
	reservationReq
}

type batchReq struct {
	Items []batchItemReq `json:"items"`
}

type batchItemReq struct {
	Fixed *reservationReq `json:"fixed,omitempty"`
	Place *placeItemReq   `json:"place,omitempty"`
}

type earliestReq struct {
	Resource        string           `json:"resource"`
	Window          intervalDTO      `json:"window"`
	DurationMinutes int64            `json:"duration_minutes"`
	Earliest        *time.Time       `json:"earliest,omitempty"`
	Demand          scheduler.Demand `json:"demand"`
}

type reservationView struct {
	ID         string            `json:"id"`
	Resource   string            `json:"resource"`
	Interval   intervalDTO       `json:"interval"`
	Demand     scheduler.Demand  `json:"demand"`
	Status     string            `json:"status"`
	AutoPlaced bool              `json:"auto_placed,omitempty"`
	Meta       map[string]string `json:"meta,omitempty"`
}

func (s *Server) view(r *scheduler.Reservation) reservationView {
	return reservationView{
		ID:         r.ID,
		Resource:   r.Resource,
		Interval:   s.intervalDTO(r.Interval),
		Demand:     append(scheduler.Demand(nil), r.Demand...),
		Status:     string(r.Status),
		AutoPlaced: r.AutoPlaced,
		Meta:       r.Meta,
	}
}

type resourceView struct {
	ID       string  `json:"id"`
	Name     string  `json:"name,omitempty"`
	Capacity []int64 `json:"capacity"`
}

type errBody struct {
	Code    string                `json:"code"`
	Message string                `json:"message"`
	Details []batchRejectItemView `json:"details,omitempty"`
}

type batchRejectItemView struct {
	Index int              `json:"index"`
	ID    string           `json:"id,omitempty"`
	Error *scheduler.Error `json:"error"`
}

// ---------- 基础工具 ----------

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, err error) {
	var be *scheduler.BatchError
	if errors.As(err, &be) {
		items := make([]batchRejectItemView, 0, len(be.Items))
		for _, it := range be.Items {
			items = append(items, batchRejectItemView{Index: it.Index, ID: it.ID, Error: it.Err})
		}
		writeJSON(w, http.StatusConflict, errBody{
			Code:    scheduler.CodeConflict,
			Message: be.Error(),
			Details: items,
		})
		return
	}
	se := scheduler.AsError(err)
	if se == nil {
		writeJSON(w, http.StatusInternalServerError, errBody{
			Code: "internal", Message: err.Error(),
		})
		return
	}
	status := http.StatusBadRequest
	switch se.Code {
	case scheduler.CodeNotFound:
		status = http.StatusNotFound
	case scheduler.CodeConflict, scheduler.CodeTerminal:
		status = http.StatusConflict
	case scheduler.CodeNoFeasibleSlot:
		status = http.StatusConflict
	}
	writeJSON(w, status, errBody{Code: se.Code, Message: se.Message})
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeJSON(w, http.StatusBadRequest, errBody{
			Code:    scheduler.CodeInvalidRequest,
			Message: "请求体不是合法 JSON: " + err.Error(),
		})
		return false
	}
	return true
}

// ---------- 路由 ----------

// Handler 返回注册好全部路由的 http.Handler。
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"ok":   true,
			"now":  s.clk.Now().Format(time.RFC3339),
			"tick": int64(s.disp.TicksAt(s.clk.Now())),
		})
	})

	mux.HandleFunc("POST /v1/resources", s.createResource)
	mux.HandleFunc("GET /v1/resources", s.listResources)
	mux.HandleFunc("GET /v1/resources/{id}", s.getResource)

	mux.HandleFunc("POST /v1/reservations", s.reserve)
	mux.HandleFunc("POST /v1/reservations:earliest-feasible", s.earliestFeasible)
	mux.HandleFunc("POST /v1/reservations:batch", s.batch)
	mux.HandleFunc("GET /v1/reservations", s.listReservations)
	mux.HandleFunc("GET /v1/reservations/{id}", s.getReservation)
	mux.HandleFunc("DELETE /v1/reservations/{id}", s.cancelReservation)

	mux.HandleFunc("GET /v1/events", s.listEvents)
	// 测试/离线回放用：把假时钟推进到给定时刻并同步推进调度。
	mux.HandleFunc("POST /test/clock/advance", s.advanceClock)

	return mux
}

// ---------- handlers ----------

func (s *Server) createResource(w http.ResponseWriter, r *http.Request) {
	var req resourceReq
	if !decode(w, r, &req) {
		return
	}
	out, err := s.sch.AddResource(scheduler.Resource{
		ID: req.ID, Name: req.Name, Capacity: req.Capacity,
	})
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, resourceView{
		ID: out.ID, Name: out.Name, Capacity: out.Capacity,
	})
}

func (s *Server) listResources(w http.ResponseWriter, r *http.Request) {
	all := s.sch.ListResources()
	out := make([]resourceView, 0, len(all))
	for _, x := range all {
		out = append(out, resourceView{ID: x.ID, Name: x.Name, Capacity: x.Capacity})
	}
	writeJSON(w, http.StatusOK, map[string]any{"resources": out})
}

func (s *Server) getResource(w http.ResponseWriter, r *http.Request) {
	out, err := s.sch.GetResource(r.PathValue("id"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resourceView{ID: out.ID, Name: out.Name, Capacity: out.Capacity})
}

func (s *Server) buildFixed(req *reservationReq) (scheduler.Request, error) {
	iv, err := s.parseInterval(req.Interval)
	if err != nil {
		return scheduler.Request{}, err
	}
	return scheduler.Request{
		ID:       req.ID,
		Resource: req.Resource,
		Interval: iv,
		Demand:   append(scheduler.Demand(nil), req.Demand...),
		Meta:     req.Meta,
	}, nil
}

func (s *Server) reserve(w http.ResponseWriter, r *http.Request) {
	var req reservationReq
	if !decode(w, r, &req) {
		return
	}
	built, err := s.buildFixed(&req)
	if err != nil {
		writeErr(w, err)
		return
	}
	out, err := s.sch.Reserve(built)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, s.view(out))
}

func (s *Server) earliestFeasible(w http.ResponseWriter, r *http.Request) {
	var req earliestReq
	if !decode(w, r, &req) {
		return
	}
	window, err := s.parseInterval(req.Window)
	if err != nil {
		writeErr(w, err)
		return
	}
	var earliest scheduler.Ticks
	if req.Earliest != nil {
		earliest, err = s.toTick(*req.Earliest)
		if err != nil {
			writeErr(w, invalidInput(err.Error()))
			return
		}
	}
	start, ok, err := s.sch.EarliestFeasible(req.Resource, window,
		scheduler.Ticks(req.DurationMinutes), earliest, req.Demand)
	if err != nil {
		writeErr(w, err)
		return
	}
	resp := map[string]any{"feasible": ok}
	if ok {
		iv := scheduler.Interval{Start: start, End: start + scheduler.Ticks(req.DurationMinutes)}
		resp["interval"] = s.intervalDTO(iv)
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) batch(w http.ResponseWriter, r *http.Request) {
	var req batchReq
	if !decode(w, r, &req) {
		return
	}
	if len(req.Items) == 0 {
		writeJSON(w, http.StatusBadRequest, errBody{
			Code: scheduler.CodeInvalidRequest, Message: "items 不能为空",
		})
		return
	}
	items := make([]scheduler.BatchItem, 0, len(req.Items))
	for i := range req.Items {
		it := req.Items[i]
		if (it.Fixed == nil) == (it.Place == nil) {
			writeJSON(w, http.StatusBadRequest, errBody{
				Code:    scheduler.CodeInvalidRequest,
				Message: fmt.Sprintf("items[%d] 必须且只能给出 fixed 或 place 之一", i),
			})
			return
		}
		var bi scheduler.BatchItem
		if it.Fixed != nil {
			built, err := s.buildFixed(it.Fixed)
			if err != nil {
				writeErr(w, err)
				return
			}
			bi.Fixed = &built
		} else {
			window, err := s.parseInterval(it.Place.Window)
			if err != nil {
				writeErr(w, err)
				return
			}
			var earliest scheduler.Ticks
			if it.Place.Earliest != nil {
				earliest, err = s.toTick(*it.Place.Earliest)
				if err != nil {
					writeErr(w, invalidInput(err.Error()))
					return
				}
			}
			bi.Place = &scheduler.PlacementRequest{
				ID:       it.Place.ID,
				Resource: it.Place.Resource,
				Window:   window,
				Duration: scheduler.Ticks(it.Place.DurationMinutes),
				Earliest: earliest,
				Demand:   append(scheduler.Demand(nil), it.Place.Demand...),
				Meta:     it.Place.Meta,
			}
		}
		items = append(items, bi)
	}
	out, err := s.sch.Batch(items)
	if err != nil {
		writeErr(w, err)
		return
	}
	views := make([]reservationView, 0, len(out))
	for _, x := range out {
		views = append(views, s.view(x))
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"count":        len(views),
		"reservations": views,
	})
}

func (s *Server) listReservations(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	f := scheduler.ListFilter{
		Resource: q.Get("resource"),
		Status:   scheduler.ReservationStatus(q.Get("status")),
	}
	all := s.sch.List(f)
	views := make([]reservationView, 0, len(all))
	for _, x := range all {
		views = append(views, s.view(x))
	}
	writeJSON(w, http.StatusOK, map[string]any{"reservations": views})
}

func (s *Server) getReservation(w http.ResponseWriter, r *http.Request) {
	out, err := s.sch.Get(r.PathValue("id"))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, s.view(out))
}

func (s *Server) cancelReservation(w http.ResponseWriter, r *http.Request) {
	out, err := s.sch.Cancel(r.PathValue("id"), s.disp.TicksAt(s.clk.Now()))
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, s.view(out))
}

// eventView 用于事件序列化（Detail 保持原样，已是可 JSON 化的类型）。
type eventView struct {
	Seq    int64     `json:"seq"`
	At     time.Time `json:"at"`
	Type   string    `json:"type"`
	Detail any       `json:"detail"`
}

func (s *Server) listEvents(w http.ResponseWriter, r *http.Request) {
	var after int64
	if v := r.URL.Query().Get("after_seq"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, errBody{
				Code: scheduler.CodeInvalidRequest, Message: "after_seq 必须是整数",
			})
			return
		}
		after = n
	}
	evs := s.sch.Events().Since(after)
	out := make([]eventView, 0, len(evs))
	for _, e := range evs {
		out = append(out, eventView{Seq: e.Seq, At: e.At, Type: e.Type, Detail: e.Detail})
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": out})
}

type advanceReq struct {
	To time.Time `json:"to"`
}

func (s *Server) advanceClock(w http.ResponseWriter, r *http.Request) {
	// 仅当注入的是 *clock.Fake 时可用——用于测试与离线演示。
	fc, ok := s.clk.(*clock.Fake)
	if !ok {
		writeJSON(w, http.StatusBadRequest, errBody{
			Code:    "clock_not_controllable",
			Message: "当前服务使用墙钟，不支持手动推进",
		})
		return
	}
	var req advanceReq
	if !decode(w, r, &req) {
		return
	}
	tick, err := s.toTick(req.To)
	if err != nil {
		writeErr(w, invalidInput(err.Error()))
		return
	}
	fc.Set(req.To)
	res := s.sch.Advance(tick)
	writeJSON(w, http.StatusOK, map[string]any{
		"now":       req.To.UTC().Format(time.RFC3339),
		"tick":      int64(tick),
		"started":   res.Started,
		"completed": res.Completed,
	})
}
