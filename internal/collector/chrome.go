package collector

import (
	"errors"
	"os"
	"os/exec"
)

// findChrome locates a usable Chromium/Chrome executable.
func findChrome() (string, error) {
	if p := os.Getenv("SV_CHROME_BIN"); p != "" {
		if _, err := os.Stat(p); err == nil {
			return p, nil
		}
	}
	for _, name := range []string{
		"google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
		"/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
	} {
		if p, err := exec.LookPath(name); err == nil {
			return p, nil
		} else if _, serr := os.Stat(name); serr == nil {
			return name, nil
		}
	}
	return "", errors.New("no Chromium/Chrome executable found; set SV_CHROME_BIN")
}
