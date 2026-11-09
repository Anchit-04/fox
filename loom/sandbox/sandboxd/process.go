package main

import (
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/windows"
)

const (
	MaxBackgroundProcesses        = 8
	BackgroundMemoryLimitMB int64 = 2048
	MaxUnreadOutputBytes          = 1024 * 1024
	StartupWindow                 = 2 * time.Second
	StopWait                      = 5 * time.Second
)

type unreadBuffer struct {
	mu      sync.Mutex
	buf     []byte
	dropped int
}

func (u *unreadBuffer) Write(p []byte) (int, error) {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.buf = append(u.buf, p...)
	if over := len(u.buf) - MaxUnreadOutputBytes; over > 0 {
		u.buf = u.buf[over:]
		u.dropped += over
	}
	return len(p), nil
}

func (u *unreadBuffer) take() string {
	u.mu.Lock()
	defer u.mu.Unlock()
	s := string(u.buf)
	if u.dropped > 0 {
		s = fmt.Sprintf("[%d earlier bytes dropped: more than %dMB went unread]\n", u.dropped, MaxUnreadOutputBytes/(1024*1024)) + s
	}
	u.buf = nil
	u.dropped = 0
	return s
}

type background struct {
	id      string
	command string
	proc    *contained
	output  *unreadBuffer
	exited  chan struct{}
	code    int
}

func (b *background) done() bool {
	select {
	case <-b.exited:
		return true
	default:
		return false
	}
}

type session struct {
	job    windows.Handle
	mu     sync.Mutex
	procs  map[string]*background
	next   int
	closed bool
}

func newSession() (*session, error) {
	job, err := newJob(0)
	if err != nil {
		return nil, fmt.Errorf("session job: %w", err)
	}
	return &session{job: job, procs: map[string]*background{}}, nil
}

func (s *session) close() {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	s.closed = true
	for id, b := range s.procs {
		windows.CloseHandle(b.proc.job)
		delete(s.procs, id)
	}
	s.mu.Unlock()
	windows.CloseHandle(s.job)
}

func (s *session) handle(req Request) Response {
	switch req.Op {
	case "", "run":
		return runContained(req.Command, DefaultTimeout, s.job, DefaultMemoryLimitMB)
	case "start":
		return s.start(req.Command)
	case "read":
		return s.read(req.Process)
	case "stop":
		return s.stop(req.Process)
	}
	return Response{ExitCode: -1, Output: fmt.Sprintf("unknown op %q", req.Op)}
}

func (s *session) running() int {
	n := 0
	for _, b := range s.procs {
		if !b.done() {
			n++
		}
	}
	return n
}

func (s *session) start(command string) Response {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return Response{ExitCode: -1, Output: "the session is closing"}
	}
	if s.running() >= MaxBackgroundProcesses {
		s.mu.Unlock()
		return Response{ExitCode: -1, Output: fmt.Sprintf(
			"%d background processes are already running in this session, the most allowed. "+
				"Stop one with stop_process first.", MaxBackgroundProcesses)}
	}
	output := &unreadBuffer{}
	proc, err := startContained(command, output, s.job, BackgroundMemoryLimitMB)
	if err != nil {
		s.mu.Unlock()
		return Response{ExitCode: -1, Output: err.Error()}
	}
	s.next++
	b := &background{
		id:      fmt.Sprintf("p%d", s.next),
		command: command,
		proc:    proc,
		output:  output,
		exited:  make(chan struct{}),
	}
	s.procs[b.id] = b
	s.mu.Unlock()

	go func() {
		b.code = exitCode(proc.cmd.Wait())
		close(b.exited)
	}()

	select {
	case <-b.exited:
	case <-time.After(StartupWindow):
	}
	return s.report(b)
}

func (s *session) report(b *background) Response {
	finished := b.done()
	resp := Response{Process: b.id, Output: b.output.take(), Status: "running"}
	if finished {
		resp.Status = "exited"
		resp.ExitCode = b.code
		s.forget(b)
	}
	return resp
}

func (s *session) forget(b *background) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.procs[b.id] != b {
		return
	}
	delete(s.procs, b.id)
	windows.CloseHandle(b.proc.job)
}

func (s *session) find(id string) (*background, Response, bool) {
	s.mu.Lock()
	b, ok := s.procs[id]
	s.mu.Unlock()
	if ok {
		return b, Response{}, true
	}
	return nil, Response{ExitCode: -1, Output: fmt.Sprintf(
		"no background process %q in this session. It may have exited and already been reported, "+
			"been stopped, or been lost when the session's shell restarted.", id)}, false
}

func (s *session) read(id string) Response {
	if id == "" {
		return s.list()
	}
	b, missing, ok := s.find(id)
	if !ok {
		return missing
	}
	return s.report(b)
}

func (s *session) list() Response {
	s.mu.Lock()
	ids := make([]string, 0, len(s.procs))
	for id := range s.procs {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return len(ids[i]) < len(ids[j]) || len(ids[i]) == len(ids[j]) && ids[i] < ids[j] })
	lines := make([]string, 0, len(ids))
	for _, id := range ids {
		b := s.procs[id]
		state := "running"
		if b.done() {
			state = fmt.Sprintf("exited with code %d, not read yet", b.code)
		}
		lines = append(lines, fmt.Sprintf("%s  %s  %s", id, state, b.command))
	}
	s.mu.Unlock()
	if len(lines) == 0 {
		return Response{Output: "no background processes in this session"}
	}
	return Response{Output: strings.Join(lines, "\n")}
}

func (s *session) stop(id string) Response {
	b, missing, ok := s.find(id)
	if !ok {
		return missing
	}
	if b.done() {
		return s.report(b)
	}
	windows.TerminateJobObject(b.proc.job, 1)
	resp := Response{Process: id, Status: "stopped", ExitCode: -1}
	select {
	case <-b.exited:
	case <-time.After(StopWait):
		resp.Output = fmt.Sprintf("[still shutting down after %v]\n", StopWait)
	}
	resp.Output += b.output.take()
	s.forget(b)
	return resp
}
