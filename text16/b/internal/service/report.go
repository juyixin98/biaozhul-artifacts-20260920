package service

import (
	"bytes"
	"fmt"
	"strings"
	"text/template"
	"time"

	"github.com/example/forensiccore/internal/chain"
	"github.com/example/forensiccore/internal/model"
)

// LimitationNotice 是哈希链能力边界声明，随每份报告输出。
const LimitationNotice = "本报告中的哈希链仅用于检测链内数据的缺失、篡改与乱序（完整性检查），" +
	"不提供可信时间戳，事件时间由本服务本地时钟产生，也不包含外部数字签名；" +
	"它不能替代可信时间服务（TSA）、外部签名或司法合规认证。" +
	"原始镜像全程仅以只读方式打开，系统不对其做任何修改；本系统不实现文件系统解析与删除恢复。"

// Report 是导出报告的结构化内容。
type Report struct {
	GeneratedAt time.Time                    `json:"generated_at"`
	Case        model.Case                   `json:"case"`
	Evidences   []model.Evidence             `json:"evidences"`
	Jobs        []model.VerifyJob            `json:"jobs"`
	Chunks      map[uint][]model.VerifyChunk `json:"chunks,omitempty"`
	Chain       []model.ChainEvent           `json:"chain"`
	ChainReport *chain.Report                `json:"chain_report"`
	Limitations string                       `json:"limitations"`
}

// BuildReport 汇总案件基线、全部复核结果与完整证据链。
func (s *Service) BuildReport(caseID uint) (*Report, error) {
	c, err := s.GetCase(caseID)
	if err != nil {
		return nil, err
	}
	evs, err := s.ListEvidence(caseID)
	if err != nil {
		return nil, err
	}
	jobs, err := s.ListJobs(caseID)
	if err != nil {
		return nil, err
	}
	chunks := map[uint][]model.VerifyChunk{}
	for _, j := range jobs {
		var cs []model.VerifyChunk
		if err := s.db.Where("job_id = ?", j.ID).Order("offset ASC").Find(&cs).Error; err != nil {
			return nil, err
		}
		chunks[j.ID] = cs
	}
	events, err := s.ListChain(caseID)
	if err != nil {
		return nil, err
	}
	cr, err := chain.Verify(s.db, caseID)
	if err != nil {
		return nil, err
	}
	return &Report{
		GeneratedAt: time.Now().UTC(),
		Case:        *c,
		Evidences:   evs,
		Jobs:        jobs,
		Chunks:      chunks,
		Chain:       events,
		ChainReport: cr,
		Limitations: LimitationNotice,
	}, nil
}

var markdownTmpl = template.Must(template.New("report").Funcs(template.FuncMap{
	"upper": strings.ToUpper,
}).Parse(`# ForensicCore 证据报告

- 案件编号：{{.Case.CaseNumber}}
- 案件标题：{{.Case.Title}}
- 案件状态：{{.Case.Status}}
- 当前保管人：{{.Case.Custodian}}
- 生成时间（UTC）：{{.GeneratedAt.Format "2006-01-02T15:04:05.000000000Z07:00"}}

## 1. 登记基线

| # | 证据 | 白名单根 | 相对路径 | 大小(字节) | SHA-256 | 登记人 |
|---|------|----------|----------|-----------|---------|--------|
{{- range .Evidences}}
| {{.ID}} | {{.Name}} | {{.RootName}} | {{.RelPath}} | {{.Size}} | {{.SHA256}} | {{.RegisteredBy}} |
{{- else}}
| （无登记证据） |||||||
{{- end}}

文件身份（用于恢复时识别同一文件，而非仅凭文件名/大小）：
{{- range .Evidences}}
- {{.Name}}: dev={{.FileDev}} ino={{.FileIno}} size={{.Size}}
{{- end}}

## 2. 完整性复核

{{- range .Jobs}}
### 作业 #{{.ID}}（证据 {{.EvidenceID}}）
- 状态：{{.Status}}
- 发起人：{{.StartedBy}}
- 开始：{{.StartedAt.Format "2006-01-02T15:04:05Z07:00"}}
{{- if .FinishedAt}}
- 完成：{{.FinishedAt.Format "2006-01-02T15:04:05Z07:00"}}
{{- end}}
- 进度：{{.ProcessedSize}} 字节；块大小：{{.ChunkSize}}
- 最终 SHA-256：{{if .FinalSHA256}}{{.FinalSHA256}}{{else}}（尚未完成）{{end}}
{{- if .LastError}}
- 错误：{{.LastError}}
{{- end}}
{{- else}}
（尚无复核作业）
{{- end}}

## 3. 证据链（append-only，共 {{len .Chain}} 条，校验：{{if .ChainReport.Healthy}}通过{{else}}未通过{{end}}）

链头：序号 {{.ChainReport.HeadSequence}}，摘要 {{if .ChainReport.HeadDigest}}{{.ChainReport.HeadDigest}}{{else}}（空链）{{end}}

| 序号 | 类型 | 操作人 | 时间(UTC) | 前一摘要 | 本事件摘要 |
|------|------|--------|-----------|----------|------------|
{{- range .Chain}}
| {{.Sequence}} | {{.EventType}} | {{.Actor}} | {{.CreatedAt.Format "2006-01-02T15:04:05.000000000Z07:00"}} | {{slice .PrevDigest 0 16}}… | {{slice .Digest 0 16}}… |
{{- else}}
| （无事件） |||||
{{- end}}

{{- if not .ChainReport.Healthy}}

### 链校验问题
{{- range .ChainReport.Issues}}
- [{{.Code}}] 序号 {{.Sequence}}：{{.Message}}
{{- end}}
{{- end}}

## 4. 能力边界与声明

> {{.Limitations}}
`))

// RenderMarkdown 渲染 Markdown 报告。
func (r *Report) RenderMarkdown() ([]byte, error) {
	var buf bytes.Buffer
	if err := markdownTmpl.Execute(&buf, r); err != nil {
		return nil, fmt.Errorf("render report: %w", err)
	}
	return buf.Bytes(), nil
}
