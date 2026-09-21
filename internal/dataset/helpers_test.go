package dataset_test

import "os"

func writeOSFile(path string, b []byte) error {
	return os.WriteFile(path, b, 0o644)
}

func removeOSPath(p string) error {
	if err := os.Remove(p); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}
