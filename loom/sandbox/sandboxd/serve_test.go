package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"testing"
	"time"
)

func startServer(t *testing.T) (io.WriteCloser, <-chan Response) {
	t.Helper()
	inReader, inWriter := io.Pipe()
	outReader, outWriter := io.Pipe()
	go serve(inReader, outWriter)

	replies := make(chan Response, 16)
	go func() {
		scanner := bufio.NewScanner(outReader)
		for scanner.Scan() {
			var resp Response
			if err := json.Unmarshal(scanner.Bytes(), &resp); err == nil {
				replies <- resp
			}
		}
	}()
	t.Cleanup(func() {
		inWriter.Close()
		outWriter.Close()
	})
	return inWriter, replies
}

func sendRequest(t *testing.T, in io.Writer, id, command string) {
	t.Helper()
	data, _ := json.Marshal(Request{ID: id, Command: command})
	if _, err := in.Write(append(data, '\n')); err != nil {
		t.Fatalf("write request %s: %v", id, err)
	}
}

func collect(t *testing.T, replies <-chan Response, n int, within time.Duration) map[string]Response {
	t.Helper()
	got := map[string]Response{}
	deadline := time.After(within)
	for len(got) < n {
		select {
		case resp := <-replies:
			got[resp.ID] = resp
		case <-deadline:
			t.Fatalf("got %d of %d replies within %v", len(got), n, within)
		}
	}
	return got
}

func TestServe_RunsRequestsInParallel(t *testing.T) {
	in, replies := startServer(t)
	ids := []string{"a", "b", "c"}

	start := time.Now()
	for _, id := range ids {
		sendRequest(t, in, id, fmt.Sprintf("ping -n 3 127.0.0.1 >nul & echo reply-%s", id))
	}
	got := collect(t, replies, len(ids), 15*time.Second)
	elapsed := time.Since(start)

	if elapsed > 4500*time.Millisecond {
		t.Errorf("three 2s commands took %v; they ran one after another", elapsed)
	}
	for _, id := range ids {
		resp, ok := got[id]
		if !ok {
			t.Fatalf("no reply for %s", id)
		}
		if strings.TrimSpace(resp.Output) != "reply-"+id {
			t.Errorf("reply for %s carried %q", id, resp.Output)
		}
	}
}

func TestServe_RepliesInFinishingOrder(t *testing.T) {
	in, replies := startServer(t)

	sendRequest(t, in, "slow", "ping -n 3 127.0.0.1 >nul & echo slow")
	sendRequest(t, in, "fast", "echo fast")

	select {
	case first := <-replies:
		if first.ID != "fast" {
			t.Errorf("first reply was %q; the fast command waited for the slow one", first.ID)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("no reply within 10s")
	}
	collect(t, replies, 1, 10*time.Second)
}

func TestServe_BadJSONGetsAnErrorReply(t *testing.T) {
	in, replies := startServer(t)

	if _, err := in.Write([]byte("not json\n")); err != nil {
		t.Fatalf("write: %v", err)
	}
	got := collect(t, replies, 1, 5*time.Second)
	resp := got[""]
	if resp.ExitCode != -1 || !strings.Contains(resp.Output, "bad request JSON") {
		t.Errorf("unexpected reply %+v", resp)
	}
}
