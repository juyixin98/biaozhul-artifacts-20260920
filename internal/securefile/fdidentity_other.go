//go:build !linux

package securefile

import (
	"os"
)

func fdIdentity(f *os.File) (Identity, error) {
	fi, err := f.Stat()
	if err != nil {
		return Identity{}, err
	}
	return Identity{
		Size:    fi.Size(),
		Mode:    fi.Mode(),
		ModTime: fi.ModTime(),
	}, nil
}
