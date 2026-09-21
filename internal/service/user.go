package service

import (
	"gorm.io/gorm"

	"proofcycle/internal/domain"
)

// UserService 用户查询。
type UserService struct {
	db *gorm.DB
}

// Get 按 ID 获取用户，不存在返回 ErrNotFound。
func (s *UserService) Get(id string) (*domain.User, error) {
	var u domain.User
	if err := s.db.First(&u, "id = ?", id).Error; err != nil {
		if err == gorm.ErrRecordNotFound {
			return nil, ErrUnknownUser
		}
		return nil, err
	}
	return &u, nil
}

// List 列出全部用户（演示/调试用）。
func (s *UserService) List() ([]domain.User, error) {
	var users []domain.User
	if err := s.db.Order("role, id").Find(&users).Error; err != nil {
		return nil, err
	}
	return users, nil
}
