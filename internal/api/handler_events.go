package api

import (
	"errors"
	"net/http"
	"sort"
	"time"

	"activityguard/internal/detection"
	"activityguard/internal/models"
	"activityguard/internal/util"

	"github.com/gin-gonic/gin"
	"gorm.io/datatypes"
)

type ingestEvent struct {
	EventID    string         `json:"event_id" binding:"required"`
	EventType  string         `json:"event_type" binding:"required"`
	EmployeeID string         `json:"employee_id" binding:"required"`
	OccurredAt time.Time      `json:"occurred_at" binding:"required"`
	Metadata   map[string]any `json:"metadata"`
}

type ingestRequest struct {
	Events []ingestEvent `json:"events" binding:"required,min=1"`
}

type itemResult struct {
	EventID    string `json:"event_id"`
	Status     string `json:"status"` // accepted | duplicate | conflict | rejected
	Reason     string `json:"reason,omitempty"`
	OccurredAt string `json:"occurred_at,omitempty"`
}

// ingestEvents 批量接收事件（上限 2000/批）：
//   - event_id 幂等：重复上报相同内容不重复入账；
//   - 同 event_id 不同内容 -> 409 conflict，整批不写入；
//   - 支持乱序/延迟上报，合法窗口为 [now-BACKFILL_WINDOW, now+FUTURE_SKEW]。
func (s *Server) ingestEvents(c *gin.Context) {
	var req ingestRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if len(req.Events) > s.cfg.BatchMaxEvents {
		c.JSON(http.StatusBadRequest, gin.H{
			"error": "batch too large",
			"limit": s.cfg.BatchMaxEvents,
			"got":   len(req.Events),
		})
		return
	}

	now := time.Now().UTC()
	oldest := now.Add(-s.cfg.BackfillWindow)
	newest := now.Add(s.cfg.FutureSkew)

	// 1) 结构与窗口校验；批次内 event_id 去重检查。
	results := make([]itemResult, 0, len(req.Events))
	seenInBatch := make(map[string]int)
	validByEmp := make(map[string][]models.Event)
	validOrder := make([]string, 0)

	addRej := func(ev ingestEvent, reason string) {
		results = append(results, itemResult{
			EventID: ev.EventID, Status: "rejected", Reason: reason,
			OccurredAt: ev.OccurredAt.UTC().Format(time.RFC3339),
		})
	}

	for _, ev := range req.Events {
		if !validEventType(ev.EventType) {
			addRej(ev, "invalid_event_type")
			continue
		}
		occ := ev.OccurredAt.UTC()
		if occ.Before(oldest) {
			addRej(ev, "outside_backfill_window")
			continue
		}
		if occ.After(newest) {
			addRej(ev, "future_event_beyond_skew")
			continue
		}
		if prev, dup := seenInBatch[ev.EventID]; dup {
			addRej(ev, "duplicate_event_id_in_batch:"+itoaIdx(prev))
			continue
		}
		seenInBatch[ev.EventID] = len(results)

		var emp models.Employee
		if err := s.db.Select("id", "department_id").
			Where("id = ? AND is_active = ?", ev.EmployeeID, true).First(&emp).Error; err != nil {
			addRej(ev, "unknown_employee")
			continue
		}

		metaBytes := canonicalJSON(ev.Metadata)
		hash := util.ContentHash(ev.EventType, ev.EmployeeID, occ.UnixNano(), metaBytes)
		row := models.Event{
			ID:          util.NewID(),
			EventID:     ev.EventID,
			EventType:   ev.EventType,
			EmployeeID:  ev.EmployeeID,
			OccurredAt:  occ,
			ReceivedAt:  now,
			Metadata:    datatypes.JSON(metaBytes),
			ContentHash: hash,
		}
		validByEmp[ev.EmployeeID] = append(validByEmp[ev.EmployeeID], row)
		validOrder = append(validOrder, ev.EventID)
	}

	// 任何结构/窗口错误都整批拒绝（原子语义，调用方修正后可整批重试）。
	if len(results) > 0 {
		c.JSON(http.StatusBadRequest, gin.H{
			"accepted": false,
			"error":    "one or more events rejected",
			"items":    results,
		})
		return
	}

	// 2) 与库内 event_id 比对：相同内容 -> duplicate；不同内容 -> 409（整批不写入）。
	incomingIDs := make([]string, 0)
	for id := range seenInBatch {
		incomingIDs = append(incomingIDs, id)
	}
	var existing []models.Event
	if err := s.db.Select("event_id", "content_hash", "event_type", "employee_id", "occurred_at").
		Where("event_id IN ?", incomingIDs).Find(&existing).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "check existing events"})
		return
	}
	hashByID := make(map[string]string, len(validByEmp)*2)
	for _, list := range validByEmp {
		for _, e := range list {
			hashByID[e.EventID] = e.ContentHash
		}
	}
	var conflicts []itemResult
	dupSet := make(map[string]bool)
	for _, ex := range existing {
		if hashByID[ex.EventID] != ex.ContentHash {
			conflicts = append(conflicts, itemResult{
				EventID: ex.EventID, Status: "conflict",
				Reason:     "same_event_id_different_content",
				OccurredAt: ex.OccurredAt.UTC().Format(time.RFC3339),
			})
		} else {
			dupSet[ex.EventID] = true
		}
	}
	if len(conflicts) > 0 {
		c.JSON(http.StatusConflict, gin.H{
			"accepted": false,
			"error":    "content conflict for existing event_id(s)",
			"items":    conflicts,
		})
		return
	}

	// 3) 交给检测引擎统一“落库 + 检测 + 标记”。
	//    引擎按 event_id 幂等：并发导入下同一 event_id 只有一行入账。
	newRows := make([]models.Event, 0)
	dupResults := make([]itemResult, 0)
	for _, list := range validByEmp {
		for _, row := range list {
			if dupSet[row.EventID] {
				dupResults = append(dupResults, itemResult{
					EventID: row.EventID, Status: "duplicate",
					OccurredAt: row.OccurredAt.Format(time.RFC3339),
				})
			} else {
				newRows = append(newRows, row)
			}
		}
	}

	// 事件先落库再检测：若检测期间进程退出，事件以 detected_at=NULL 保留，
	// 启动恢复 / 定时 pending_recovery 会补算，事件不丢。
	_, err := s.engine.ProcessEvents(newRows)
	if err != nil {
		if errors.Is(err, detection.ErrContentConflict) {
			c.JSON(http.StatusConflict, gin.H{
				"accepted": false,
				"error":    "content conflict for existing event_id(s)",
			})
			return
		}
		c.JSON(http.StatusOK, gin.H{
			"accepted":        true,
			"accepted_count":  0,
			"duplicate_count": len(dupResults),
			"detected":        false,
			"detection_error": err.Error(),
			"backfill_window": gin.H{
				"oldest_allowed": oldest.Format(time.RFC3339),
				"newest_allowed": newest.Format(time.RFC3339),
			},
		})
		return
	}

	// 精确分类：按内部 ID 判断哪些行确实由本次请求插入
	//（并发下另一请求插入的同行 event_id 不会有我们生成的内部 ID）。
	internalIDs := make([]string, 0, len(newRows))
	for _, r := range newRows {
		internalIDs = append(internalIDs, r.ID)
	}
	var justInserted []models.Event
	if err := s.db.Where("id IN ?", internalIDs).Find(&justInserted).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "classify inserted: " + err.Error()})
		return
	}
	justSet := make(map[string]bool, len(justInserted))
	for _, ji := range justInserted {
		justSet[ji.EventID] = true
	}
	finalDup := append([]itemResult{}, dupResults...)
	acceptedResults := make([]itemResult, 0, len(justSet))
	for _, r := range newRows {
		if justSet[r.EventID] {
			acceptedResults = append(acceptedResults, itemResult{
				EventID: r.EventID, Status: "accepted",
				OccurredAt: r.OccurredAt.Format(time.RFC3339),
			})
		} else {
			finalDup = append(finalDup, itemResult{
				EventID: r.EventID, Status: "duplicate",
				OccurredAt: r.OccurredAt.Format(time.RFC3339),
			})
		}
	}

	all := append(acceptedResults, finalDup...)
	sort.Slice(all, func(i, j int) bool {
		// 按请求顺序输出，便于调用方对照
		return indexOrMax(validOrder, all[i].EventID) < indexOrMax(validOrder, all[j].EventID)
	})

	c.JSON(http.StatusOK, gin.H{
		"accepted":        true,
		"accepted_count":  len(acceptedResults),
		"duplicate_count": len(finalDup),
		"detected":        len(acceptedResults) > 0,
		"items":           all,
		"backfill_window": gin.H{
			"oldest_allowed": oldest.Format(time.RFC3339),
			"newest_allowed": newest.Format(time.RFC3339),
		},
	})
}

func validEventType(t string) bool {
	return t == models.EventLogin || t == models.EventFileDownload || t == models.EventUSB
}

func itoaIdx(i int) string {
	if i == 0 {
		return "0"
	}
	var b [20]byte
	p := len(b)
	for i > 0 {
		p--
		b[p] = byte('0' + i%10)
		i /= 10
	}
	return string(b[p:])
}

func indexOrMax(xs []string, x string) int {
	for i, v := range xs {
		if v == x {
			return i
		}
	}
	return len(xs) + 1
}
