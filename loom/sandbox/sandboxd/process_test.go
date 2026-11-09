package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func newTestSession(t *testing.T) *session {
	t.Helper()
	s, err := newSession()
	if err != nil {
		t.Fatalf("newSession: %v", err)
	}
	t.Cleanup(s.close)
	return s
}

func ticker(count int) string {
	return fmt.Sprintf(`for /L %%i in (1,1,%d) do @(echo tick %%i & ping -n 2 127.0.0.1 >nul)`, count)
}

func lateMarker(path string, seconds int) string {
	return fmt.Sprintf(`ping -n %d 127.0.0.1 >nul & echo late > "%s"`, seconds+1, path)
}

func TestProcess_KeepsRunningAndReadsNewOutput(t *testing.T) {
	s := newTestSession(t)

	started := s.start(ticker(30))
	if started.Status != "running" || started.Process != "p1" {
		t.Fatalf("start = %+v, want p1 running", started)
	}
	if !strings.Contains(started.Output, "tick 1") {
		t.Fatalf("start output %q, want the first tick", started.Output)
	}

	time.Sleep(2500 * time.Millisecond)
	later := s.read("p1")
	if later.Status != "running" {
		t.Fatalf("read = %+v, want still running", later)
	}
	if strings.Contains(later.Output, "tick 1\r\n") || !strings.Contains(later.Output, "tick") {
		t.Fatalf("read output %q, want only ticks printed since the start", later.Output)
	}

	stopped := s.stop("p1")
	if stopped.Status != "stopped" {
		t.Fatalf("stop = %+v, want stopped", stopped)
	}
	if gone := s.read("p1"); gone.ExitCode != -1 || !strings.Contains(gone.Output, "no background process") {
		t.Fatalf("read after stop = %+v, want not found", gone)
	}
}

func TestProcess_QuickExitIsReportedOnceWithItsCode(t *testing.T) {
	s := newTestSession(t)

	resp := s.start(`echo hi & exit /b 3`)
	if resp.Status != "exited" || resp.ExitCode != 3 || !strings.Contains(resp.Output, "hi") {
		t.Fatalf("start = %+v, want exited with code 3 and its output", resp)
	}
	if again := s.read(resp.Process); !strings.Contains(again.Output, "no background process") {
		t.Fatalf("second read = %+v, want not found", again)
	}
}

func TestProcess_ExitWhileRunningShowsOnNextRead(t *testing.T) {
	s := newTestSession(t)

	resp := s.start(`ping -n 4 127.0.0.1 >nul & echo finished & exit /b 5`)
	if resp.Status != "running" {
		t.Fatalf("start = %+v, want running", resp)
	}
	if list := s.read(""); !strings.Contains(list.Output, "p1  running") {
		t.Fatalf("list = %q, want p1 running", list.Output)
	}

	time.Sleep(3 * time.Second)
	if list := s.read(""); !strings.Contains(list.Output, "exited with code 5, not read yet") {
		t.Fatalf("list = %q, want p1 exited and unread", list.Output)
	}
	done := s.read("p1")
	if done.Status != "exited" || done.ExitCode != 5 || !strings.Contains(done.Output, "finished") {
		t.Fatalf("read = %+v, want exited 5 with its last output", done)
	}
}

func TestProcess_AtMostEightRunAtOnce(t *testing.T) {
	s := newTestSession(t)

	var wg sync.WaitGroup
	for i := 0; i < MaxBackgroundProcesses; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			s.start(`ping -n 30 127.0.0.1 >nul`)
		}()
	}
	wg.Wait()

	refused := s.start(`echo ninth`)
	if refused.ExitCode != -1 || !strings.Contains(refused.Output, "the most allowed") {
		t.Fatalf("ninth start = %+v, want refused", refused)
	}

	s.stop("p1")
	if allowed := s.start(`echo ninth`); allowed.Status != "exited" {
		t.Fatalf("start after a stop = %+v, want it to run", allowed)
	}
}

func TestLateMarker_IsWrittenWhenNothingStopsIt(t *testing.T) {
	s := newTestSession(t)
	marker := filepath.Join(t.TempDir(), "marker.txt")

	s.start(fmt.Sprintf(`start /b cmd /c "%s"`, lateMarker(marker, 1)))

	time.Sleep(3 * time.Second)
	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("the marker command never wrote its file, so the kill tests prove nothing: %v", err)
	}
}

func TestProcess_StopKillsWhatTheProcessStarted(t *testing.T) {
	s := newTestSession(t)
	marker := filepath.Join(t.TempDir(), "marker.txt")

	resp := s.start(fmt.Sprintf(`start /b cmd /c "%s" & ping -n 30 127.0.0.1 >nul`, lateMarker(marker, 4)))
	if resp.Status != "running" {
		t.Fatalf("start = %+v, want running", resp)
	}
	s.stop(resp.Process)

	time.Sleep(4 * time.Second)
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("a child of the stopped process kept running and wrote its marker")
	}
}

func TestSession_CloseKillsEveryBackgroundProcess(t *testing.T) {
	s, err := newSession()
	if err != nil {
		t.Fatalf("newSession: %v", err)
	}
	dir := t.TempDir()
	markers := []string{filepath.Join(dir, "a.txt"), filepath.Join(dir, "b.txt")}
	for _, m := range markers {
		if resp := s.start(lateMarker(m, 6)); resp.Status != "running" {
			t.Fatalf("start = %+v, want running", resp)
		}
	}

	s.close()

	time.Sleep(4 * time.Second)
	for _, m := range markers {
		if _, err := os.Stat(m); err == nil {
			t.Fatalf("%s was written after the session closed", filepath.Base(m))
		}
	}
	if resp := s.start(`echo late`); !strings.Contains(resp.Output, "closing") {
		t.Fatalf("start after close = %+v, want refused", resp)
	}
}

func TestRunContained_CommandsJoinTheSessionJob(t *testing.T) {
	s := newTestSession(t)
	marker := filepath.Join(t.TempDir(), "marker.txt")

	go s.handle(Request{Command: lateMarker(marker, 4)})
	time.Sleep(time.Second)
	s.close()

	time.Sleep(4 * time.Second)
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("a one-shot command outlived its session")
	}
}

func TestUnreadBuffer_KeepsTheNewestMegabyte(t *testing.T) {
	var u unreadBuffer
	u.Write([]byte(strings.Repeat("a", MaxUnreadOutputBytes)))
	u.Write([]byte("newest"))

	got := u.take()
	if !strings.HasPrefix(got, "[6 earlier bytes dropped") || !strings.HasSuffix(got, "newest") {
		t.Fatalf("take() starts %q and ends %q", got[:40], got[len(got)-10:])
	}
	if again := u.take(); again != "" {
		t.Fatalf("second take() = %q, want empty", again)
	}
}
