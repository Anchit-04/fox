// sandboxd runs shell commands on behalf of the Python agent, contained in
// a Windows Job Object (hard memory ceiling, guaranteed process-tree kill
// on timeout). One JSON request per line in, one JSON response per line out.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sync"
	"time"

	"golang.org/x/sys/windows"
)

type Request struct {
	ID      string `json:"id"`
	Op      string `json:"op"`
	Command string `json:"command"`
	Process string `json:"process"`
}

type Response struct {
	ID       string `json:"id"`
	ExitCode int    `json:"exit_code"`
	Output   string `json:"output"`
	Process  string `json:"process,omitempty"`
	Status   string `json:"status,omitempty"`
}

const MaxCapturedOutputBytes = 2 * 1024 * 1024 // 2MB

type limitedWriter struct {
	mu        sync.Mutex
	buf       bytes.Buffer
	limit     int
	truncated bool
}

func (w *limitedWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	remaining := w.limit - w.buf.Len()
	if remaining <= 0 {
		w.truncated = true
		return len(p), nil
	}
	if len(p) > remaining {
		w.buf.Write(p[:remaining])
		w.truncated = true
		return len(p), nil
	}
	return w.buf.Write(p)
}

func (w *limitedWriter) String() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	s := w.buf.String()
	if w.truncated {
		s += fmt.Sprintf("\n... [output truncated at sandboxd's %dMB buffer cap]", MaxCapturedOutputBytes/(1024*1024))
	}
	return s
}

type responder struct {
	mu  sync.Mutex
	out io.Writer
}

func (r *responder) send(resp Response) {
	data, err := json.Marshal(resp)
	if err != nil {
		data = []byte(fmt.Sprintf(`{"id":%q,"exit_code":-1,"output":"internal error marshaling response"}`, resp.ID))
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.out.Write(append(data, '\n'))
}

func serve(in io.Reader, out io.Writer) error {
	sess, err := newSession()
	if err != nil {
		return err
	}
	defer sess.close()

	reader := bufio.NewReader(in)
	replies := &responder{out: out}

	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			return nil
		}

		var req Request
		if err := json.Unmarshal([]byte(line), &req); err != nil {
			replies.send(Response{ExitCode: -1, Output: fmt.Sprintf("bad request JSON: %v", err)})
			continue
		}

		go func(req Request) {
			resp := sess.handle(req)
			resp.ID = req.ID
			replies.send(resp)
		}(req)
	}
}

func main() {
	if err := serve(os.Stdin, os.Stdout); err != nil {
		fmt.Fprintf(os.Stderr, "sandboxd: %v\n", err)
		os.Exit(1)
	}
}

const (
	DefaultTimeout             = 30 * time.Second
	DefaultMemoryLimitMB int64 = 512
)

func runContained(command string, timeout time.Duration, parent windows.Handle, memoryLimitMB int64) Response {
	buf := &limitedWriter{limit: MaxCapturedOutputBytes}
	proc, err := startContained(command, buf, parent, memoryLimitMB)
	if err != nil {
		return Response{ExitCode: -1, Output: err.Error()}
	}
	defer windows.CloseHandle(proc.job)

	done := make(chan error, 1)
	go func() {
		done <- proc.cmd.Wait()
	}()

	select {
	case err := <-done:
		code := exitCode(err)
		if code == -1 {
			return Response{ExitCode: -1, Output: fmt.Sprintf("wait failed: %v", err)}
		}
		return Response{ExitCode: code, Output: buf.String()}

	case <-time.After(timeout):
		windows.TerminateJobObject(proc.job, 1)
		return Response{
			ExitCode: -1,
			Output:   fmt.Sprintf("command timed out after %v; output so far:\n%s", timeout, buf.String()),
		}
	}
}
