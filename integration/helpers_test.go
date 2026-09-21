package integration

import (
	"encoding/json"
	"testing"

	"proofcycle/internal/domain"
	"proofcycle/internal/service"
)

// twoReviewerEnvPublic 暴露给 HTTP 测试使用的搭建辅助。
func twoReviewerEnvPublic(t *testing.T) (*testEnv, *domain.Job, []string) {
	return twoReviewerEnv(t)
}

type httpItemDTO struct {
	SnapshotItemID string `json:"snapshot_item_id"`
	Result         string `json:"result"`
	FailReason     string `json:"fail_reason,omitempty"`
}

type httpSubmitBody struct {
	VersionNo int           `json:"version_no"`
	Items     []httpItemDTO `json:"items"`
}

// encodeReviewSubmit 把服务层输入编码成 HTTP 请求 JSON。
func encodeReviewSubmit(versionNo int, items []service.ItemInput) ([]byte, error) {
	dtos := make([]httpItemDTO, len(items))
	for i, it := range items {
		dtos[i] = httpItemDTO{
			SnapshotItemID: it.SnapshotItemID,
			Result:         string(it.Result),
			FailReason:     it.FailReason,
		}
	}
	return json.Marshal(httpSubmitBody{VersionNo: versionNo, Items: dtos})
}
