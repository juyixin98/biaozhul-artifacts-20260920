// Package service 是 ForensicCore 的业务层：案件/证据登记、只读基线计算、
// 持久化可恢复的完整性复核作业、append-only 证据哈希链与报告导出。
package service

import (
	"context"
	"errors"
	"fmt"
	"path"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/unix"
	"gorm.io/gorm"

	"github.com/example/forensiccore/internal/chain"
	"github.com/example/forensiccore/internal/config"
	"github.com/example/forensiccore/internal/hashfile"
	"github.com/example/forensiccore/internal/model"
	"github.com/example/forensiccore/internal/safeopen"
)

// RegisterObserver 在登记两遍哈希的第一遍中，每读完一块后回调。
// 测试用它在“读取过程中”改写文件。
type RegisterObserver func(evidenceName string, offset int64, chunk []byte) error

// JobHook 在复核作业每处理完一块后回调，返回非 nil（通常是 ctx 取消）
// 会使作业停在当前进度，状态为 interrupted。测试用它制造中断与恢复。
type JobHook func(jobID uint, processed int64, total int64) error

// Service 持有数据库、白名单根目录与作业执行器。
type Service struct {
	db    *gorm.DB
	cfg   *config.Config
	roots map[string]*safeopen.Root

	queue  chan uint
	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup

	mu       sync.Mutex
	enqueued map[uint]bool // 防止同一作业重复入队

	regObserver RegisterObserver
	jobHook     JobHook
}

// New 构造服务并打开全部白名单根目录（只读句柄）。
func New(db *gorm.DB, cfg *config.Config) (*Service, error) {
	roots := map[string]*safeopen.Root{}
	for name, p := range cfg.Roots {
		r, err := safeopen.OpenRoot(name, p)
		if err != nil {
			return nil, fmt.Errorf("open evidence root %q (%s): %w", name, p, err)
		}
		roots[name] = r
	}
	ctx, cancel := context.WithCancel(context.Background())
	return &Service{
		db:       db,
		cfg:      cfg,
		roots:    roots,
		queue:    make(chan uint, 256),
		ctx:      ctx,
		cancel:   cancel,
		enqueued: map[uint]bool{},
	}, nil
}

// Start 启动后台 worker，并把上次中断的作业重新排队。
func (s *Service) Start() error {
	for i := 0; i < 2; i++ {
		s.wg.Add(1)
		go s.worker(i)
	}
	return s.requeueInterrupted()
}

// Close 停止 worker 并释放根目录句柄。
func (s *Service) Close() {
	s.cancel()
	s.wg.Wait()
	for _, r := range s.roots {
		_ = r.Close()
	}
}

// SetRegisterObserver 注入登记读取观察器（测试用）。
func (s *Service) SetRegisterObserver(o RegisterObserver) { s.regObserver = o }

// SetJobHook 注入复核作业块级钩子（测试用）。
func (s *Service) SetJobHook(h JobHook) { s.jobHook = h }

// ---------- 认证 ----------

// Authenticate 校验本地静态账号，成功返回用户名与角色。
func (s *Service) Authenticate(username, password string) (string, string, error) {
	u, ok := s.cfg.Users[username]
	if !ok || u.Password != password {
		return "", "", kindErr(KindUnauthorized, "invalid username or password")
	}
	return username, u.Role, nil
}

// UserRole 返回账号角色，未知用户返回空串。
func (s *Service) UserRole(username string) string {
	if u, ok := s.cfg.Users[username]; ok {
		return u.Role
	}
	return ""
}

// JWTSecret 暴露签名密钥给 API 层。
func (s *Service) JWTSecret() []byte { return s.cfg.JWTSecret }

// TokenTTL 返回令牌有效期。
func (s *Service) TokenTTL() time.Duration {
	return time.Duration(s.cfg.TokenTTLHours) * time.Hour
}

// ---------- 案件 ----------

// CreateCaseInput 创建案件入参。
type CreateCaseInput struct {
	CaseNumber string
	Title      string
	Custodian  string
	Actor      string
}

// CreateCase 登记新案件（调查员）。
func (s *Service) CreateCase(in CreateCaseInput) (*model.Case, error) {
	if strings.TrimSpace(in.CaseNumber) == "" || strings.TrimSpace(in.Title) == "" ||
		strings.TrimSpace(in.Custodian) == "" {
		return nil, kindErr(KindValidation, "case_number, title and custodian are required")
	}
	c := &model.Case{
		CaseNumber: strings.TrimSpace(in.CaseNumber),
		Title:      strings.TrimSpace(in.Title),
		Status:     model.CaseStatusOpen,
		Custodian:  strings.TrimSpace(in.Custodian),
		CreatedBy:  in.Actor,
	}
	if err := s.db.Create(c).Error; err != nil {
		if isDuplicateKey(err) {
			return nil, kindErr(KindConflict, "case_number already exists")
		}
		return nil, err
	}
	return c, nil
}

// GetCase 查询案件。
func (s *Service) GetCase(id uint) (*model.Case, error) {
	var c model.Case
	if err := s.db.First(&c, id).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, kindErr(KindNotFound, "case %d not found", id)
		}
		return nil, err
	}
	return &c, nil
}

// ListCases 列出案件。
func (s *Service) ListCases() ([]model.Case, error) {
	var cs []model.Case
	if err := s.db.Order("id DESC").Find(&cs).Error; err != nil {
		return nil, err
	}
	return cs, nil
}

// ---------- 证据登记 ----------

// RegisterEvidenceInput 登记证据入参。
type RegisterEvidenceInput struct {
	CaseID   uint
	RootName string
	RelPath  string
	Name     string // 证据在案件内的显示名
	Actor    string
}

// RegisterEvidence 对白名单目录内的 raw/dd 镜像执行只读分块基线登记。
//
// 流程：安全打开（防路径/符号链接越界）→ fstat 身份快照 → 两遍 SHA-256，
// 期间任何变化都会失败，不写入任何基线记录 → 事务写入证据并追加登记事件。
func (s *Service) RegisterEvidence(in RegisterEvidenceInput) (*model.Evidence, error) {
	root, relPath, name, err := s.prepareEvidencePath(in.RootName, in.RelPath, in.Name)
	if err != nil {
		return nil, err
	}
	if _, err := s.GetCase(in.CaseID); err != nil {
		return nil, err
	}

	fd, st, err := root.Open(relPath, unix.O_RDONLY)
	if err != nil {
		return nil, mapOpenErr(err)
	}
	defer unix.Close(fd)
	before := hashfile.FromFileStat(st)

	observer := hashfile.ChunkObserver(nil)
	if s.regObserver != nil {
		observer = func(offset int64, chunk []byte) error {
			return s.regObserver(name, offset, chunk)
		}
	}
	base, err := hashfile.ComputeBaseline(fd, before, s.cfg.ChunkSize, observer)
	if err != nil {
		if errors.Is(err, hashfile.ErrFileChanged) {
			return nil, kindErr(KindFileChanged,
				"evidence %s changed while hashing; baseline NOT saved: %v", name, err)
		}
		return nil, err
	}

	var ev model.Evidence
	txErr := s.db.Transaction(func(tx *gorm.DB) error {
		ev = model.Evidence{
			CaseID:       in.CaseID,
			Name:         name,
			RootName:     root.Name,
			RelPath:      relPath,
			Size:         base.Size,
			SHA256:       strings.ToUpper(base.SHA256),
			FileDev:      base.Identity.Dev,
			FileIno:      base.Identity.Ino,
			RegisteredBy: in.Actor,
		}
		if err := tx.Create(&ev).Error; err != nil {
			if isDuplicateKey(err) {
				return kindErr(KindConflict, "evidence name already exists in this case")
			}
			return err
		}
		payload := RegisterPayload{
			EvidenceID: ev.ID,
			Name:       ev.Name,
			RootName:   ev.RootName,
			RelPath:    ev.RelPath,
			Size:       ev.Size,
			SHA256:     ev.SHA256,
			FileDev:    ev.FileDev,
			FileIno:    ev.FileIno,
		}
		_, err := chain.Append(tx, chain.AppendInput{
			CaseID:  in.CaseID,
			Type:    model.EventRegister,
			Actor:   in.Actor,
			Payload: mustJSON(payload),
		})
		return err
	})
	if txErr != nil {
		var ke *Error
		if errors.As(txErr, &ke) {
			return nil, txErr
		}
		if isDuplicateKey(txErr) {
			return nil, kindErr(KindConflict, "evidence name already exists in this case")
		}
		return nil, txErr
	}
	return &ev, nil
}

// prepareEvidencePath 校验白名单根、相对路径与 raw/dd 扩展名。
func (s *Service) prepareEvidencePath(rootName, relPath, name string) (*safeopen.Root, string, string, error) {
	root, ok := s.roots[rootName]
	if !ok {
		return nil, "", "", kindErr(KindValidation,
			"unknown whitelist root %q; allowed: %s", rootName, strings.Join(s.rootNames(), ","))
	}
	relPath = strings.TrimSpace(relPath)
	if relPath == "" {
		return nil, "", "", kindErr(KindValidation, "rel_path is required")
	}
	// 在任何归一化之前做词法校验：拒绝绝对路径、空分量、"."、".."。
	cleaned, perr := safeopen.ValidateRelPath(relPath)
	if perr != nil {
		return nil, "", "", kindErr(KindValidation, "%v", perr)
	}
	ext := strings.ToLower(path.Ext(cleaned))
	if ext != ".raw" && ext != ".dd" {
		return nil, "", "", kindErr(KindValidation,
			"only raw disk images with .raw/.dd extension are accepted, got %q", ext)
	}
	if strings.TrimSpace(name) == "" {
		name = path.Base(cleaned)
	}
	return root, cleaned, strings.TrimSpace(name), nil
}

func (s *Service) rootNames() []string {
	names := make([]string, 0, len(s.roots))
	for n := range s.roots {
		names = append(names, n)
	}
	return names
}

func mapOpenErr(err error) error {
	switch {
	case errors.Is(err, safeopen.ErrPathTraversal):
		return kindErr(KindValidation, "%v", err)
	case errors.Is(err, safeopen.ErrSymlink):
		return kindErr(KindValidation, "%v", err)
	case errors.Is(err, safeopen.ErrNotRegular):
		return kindErr(KindValidation, "%v", err)
	case errors.Is(err, safeopen.ErrNotDirectory):
		return kindErr(KindValidation, "%v", err)
	case errors.Is(err, safeopen.ErrNotFound):
		return kindErr(KindNotFound, "%v", err)
	case errors.Is(err, safeopen.ErrPermission):
		return kindErr(KindForbidden, "%v", err)
	default:
		return kindErr(KindInternal, "open evidence: %v", err)
	}
}

// GetEvidence 查询证据。
func (s *Service) GetEvidence(caseID, id uint) (*model.Evidence, error) {
	var ev model.Evidence
	if err := s.db.Where("case_id = ? AND id = ?", caseID, id).First(&ev).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, kindErr(KindNotFound, "evidence %d not found in case %d", id, caseID)
		}
		return nil, err
	}
	return &ev, nil
}

// ListEvidence 列出案件下证据。
func (s *Service) ListEvidence(caseID uint) ([]model.Evidence, error) {
	if _, err := s.GetCase(caseID); err != nil {
		return nil, err
	}
	var evs []model.Evidence
	if err := s.db.Where("case_id = ?", caseID).Order("id ASC").Find(&evs).Error; err != nil {
		return nil, err
	}
	return evs, nil
}

// isDuplicateKey 粗略识别唯一键冲突（MySQL 1062 / SQLite UNIQUE）。
func isDuplicateKey(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "Duplicate entry") || strings.Contains(msg, "UNIQUE constraint")
}
