//go:build !windows

package platform

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
)

func OpenWindow(args []string) error {
	exe, err := os.Executable()
	if err != nil {
		return fmt.Errorf("find fox executable: %w", err)
	}
	term, err := terminal()
	if err != nil {
		return err
	}
	cmd := exec.Command(term, append([]string{"-e", exe}, args...)...)
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start %s: %w", term, err)
	}
	return cmd.Process.Release()
}

func terminal() (string, error) {
	if t := os.Getenv("TERMINAL"); t != "" {
		return t, nil
	}
	if t, err := exec.LookPath("x-terminal-emulator"); err == nil {
		return t, nil
	}
	return "", errors.New("no terminal found, set $TERMINAL")
}
