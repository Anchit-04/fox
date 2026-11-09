package main

import (
	"errors"
	"fmt"
	"io"
	"os/exec"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

type contained struct {
	cmd *exec.Cmd
	job windows.Handle
}

func newJob(memoryLimitMB int64) (windows.Handle, error) {
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return 0, fmt.Errorf("failed to create job object: %w", err)
	}
	info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{
		BasicLimitInformation: windows.JOBOBJECT_BASIC_LIMIT_INFORMATION{
			LimitFlags: windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
		},
	}
	if memoryLimitMB > 0 {
		info.BasicLimitInformation.LimitFlags |= windows.JOB_OBJECT_LIMIT_JOB_MEMORY
		info.JobMemoryLimit = uintptr(memoryLimitMB * 1024 * 1024)
	}
	_, err = windows.SetInformationJobObject(
		job,
		windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)),
		uint32(unsafe.Sizeof(info)),
	)
	if err != nil {
		windows.CloseHandle(job)
		return 0, fmt.Errorf("failed to configure job object: %w", err)
	}
	return job, nil
}

func startContained(command string, out io.Writer, parent windows.Handle, memoryLimitMB int64) (*contained, error) {
	cmd := exec.Command("cmd")
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CmdLine:       `/C ` + command,
		CreationFlags: windows.CREATE_SUSPENDED,
	}
	cmd.Stdout = out
	cmd.Stderr = out

	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("failed to start command: %w", err)
	}

	job, err := place(cmd.Process.Pid, parent, memoryLimitMB)
	if err != nil {
		cmd.Process.Kill()
		cmd.Wait()
		return nil, err
	}

	if err := resume(uint32(cmd.Process.Pid)); err != nil {
		windows.TerminateJobObject(job, 1)
		windows.CloseHandle(job)
		cmd.Wait()
		return nil, fmt.Errorf("failed to resume command: %w", err)
	}
	return &contained{cmd: cmd, job: job}, nil
}

func place(pid int, parent windows.Handle, memoryLimitMB int64) (windows.Handle, error) {
	process, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(pid))
	if err != nil {
		return 0, fmt.Errorf("failed to open process handle: %w", err)
	}
	defer windows.CloseHandle(process)

	if parent != 0 {
		if err := windows.AssignProcessToJobObject(parent, process); err != nil {
			return 0, fmt.Errorf("failed to assign process to the session job: %w", err)
		}
	}

	job, err := newJob(memoryLimitMB)
	if err != nil {
		return 0, err
	}
	if err := windows.AssignProcessToJobObject(job, process); err != nil {
		windows.CloseHandle(job)
		return 0, fmt.Errorf("failed to assign process to job: %w", err)
	}
	return job, nil
}

func resume(pid uint32) error {
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPTHREAD, 0)
	if err != nil {
		return err
	}
	defer windows.CloseHandle(snapshot)

	entry := windows.ThreadEntry32{Size: uint32(unsafe.Sizeof(windows.ThreadEntry32{}))}
	resumed := 0
	for walk := windows.Thread32First(snapshot, &entry); walk == nil; walk = windows.Thread32Next(snapshot, &entry) {
		if entry.OwnerProcessID != pid {
			continue
		}
		thread, err := windows.OpenThread(windows.THREAD_SUSPEND_RESUME, false, entry.ThreadID)
		if err != nil {
			return err
		}
		_, err = windows.ResumeThread(thread)
		windows.CloseHandle(thread)
		if err != nil {
			return err
		}
		resumed++
	}
	if resumed == 0 {
		return fmt.Errorf("no thread found for process %d", pid)
	}
	return nil
}

func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return exitErr.ExitCode()
	}
	return -1
}
