package platform

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
)

const createNewConsole = 0x00000010

func OpenWindow(args []string) error {
	exe, err := os.Executable()
	if err != nil {
		return fmt.Errorf("find fox executable: %w", err)
	}
	if os.Getenv("WT_SESSION") != "" {
		if wt, ok := windowsTerminal(); ok {
			cmd := exec.Command(wt, append([]string{"-w", "0", "new-tab", exe}, args...)...)
			if err := cmd.Start(); err == nil {
				return cmd.Process.Release()
			}
		}
	}
	cmd := exec.Command(exe, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNewConsole}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start window: %w", err)
	}
	return cmd.Process.Release()
}

func windowsTerminal() (string, bool) {
	if wt, err := exec.LookPath("wt.exe"); err == nil {
		return wt, true
	}
	wt := filepath.Join(os.Getenv("LOCALAPPDATA"), "Microsoft", "WindowsApps", "wt.exe")
	if _, err := os.Stat(wt); err == nil {
		return wt, true
	}
	return "", false
}
