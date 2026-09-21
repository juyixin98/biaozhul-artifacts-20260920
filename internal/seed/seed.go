package seed

import (
	"errors"

	"proofcycle/internal/auth"
	"proofcycle/internal/models"

	"gorm.io/gorm"
)

// DemoAccount is a seeded demo user with deterministic credentials so the
// demo script can run without an out-of-band login.
type DemoAccount struct {
	Username string
	Password string
	Role     string
	Token    string // fixed, long-lived API token
}

// DemoUsers are the demonstration accounts: one designer, one PM and up to
// eight reviewers (the roster ceiling for a single job). Tokens are fixed
// 64-hex-char API tokens so demo scripts can authenticate without a login
// round-trip (logging in rotates them).
var DemoUsers = []DemoAccount{
	{Username: "dana", Password: "designer123", Role: models.RoleDesigner, Token: "0000000000000000000000000000000000000000000000000000000000000aa1"},
	{Username: "priya", Password: "pm1234567", Role: models.RolePM, Token: "0000000000000000000000000000000000000000000000000000000000000bb2"},
	{Username: "rev1", Password: "review123", Role: models.RoleReviewer, Token: "00000000000000000000000000000000000000000000000000000000000001c3"},
	{Username: "rev2", Password: "review123", Role: models.RoleReviewer, Token: "00000000000000000000000000000000000000000000000000000000000002d4"},
	{Username: "rev3", Password: "review123", Role: models.RoleReviewer, Token: "00000000000000000000000000000000000000000000000000000000000003e5"},
	{Username: "rev4", Password: "review123", Role: models.RoleReviewer, Token: "00000000000000000000000000000000000000000000000000000000000004f6"},
	{Username: "rev5", Password: "review123", Role: models.RoleReviewer, Token: "00000000000000000000000000000000000000000000000000000000000005a7"},
	{Username: "rev6", Password: "review123", Role: models.RoleReviewer, Token: "00000000000000000000000000000000000000000000000000000000000006b8"},
	{Username: "rev7", Password: "review123", Role: models.RoleReviewer, Token: "00000000000000000000000000000000000000000000000000000000000007c9"},
	{Username: "rev8", Password: "review123", Role: models.RoleReviewer, Token: "00000000000000000000000000000000000000000000000000000000000008da"},
}

// Users performs an idempotent upsert of the demo accounts. Existing users
// keep their password hash; the demo token is refreshed.
func Users(gdb *gorm.DB) error {
	for _, d := range DemoUsers {
		var u models.User
		err := gdb.Where("username = ?", d.Username).First(&u).Error
		switch {
		case errors.Is(err, gorm.ErrRecordNotFound):
			hash, herr := auth.HashPassword(d.Password)
			if herr != nil {
				return herr
			}
			u = models.User{
				Username:     d.Username,
				PasswordHash: hash,
				Role:         d.Role,
				Token:        d.Token,
			}
			if err := gdb.Create(&u).Error; err != nil {
				return err
			}
		case err != nil:
			return err
		default:
			if err := gdb.Model(&u).Updates(map[string]any{
				"role":  d.Role,
				"token": d.Token,
			}).Error; err != nil {
				return err
			}
		}
	}
	return nil
}
