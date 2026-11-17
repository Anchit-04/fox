package state

import (
	"strings"
	"testing"
)

func TestASignedOutExecutorWaitsForASignIn(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	feed(t, s, `{"type":"task_created","session_id":"s1","seq":2,"task_id":"t1","description":"d","lane":"write","scope":[]}`)
	feed(t, s, `{"type":"task_created","session_id":"s1","seq":3,"task_id":"t2","description":"d","lane":"write","scope":[]}`)
	s.AddUserMessage("s1", "t1", "first")
	s.AddUserMessage("s1", "t2", "other")
	s.AddUserMessage("s1", "t1", "Hi")
	feed(t, s, `{"session_id":"s1","seq":4,"type":"model_error","task_id":"t1","model_key":"claude-code","kind":"auth","message":"Failed to authenticate"}`)

	sess := s.Session("s1")
	last := sess.Entries[len(sess.Entries)-1]
	if last.Kind != ErrorEntry || !strings.Contains(last.Text, "ctrl+l") {
		t.Fatalf("last = %+v", last)
	}
	if got := s.NeedsSignIn("s1", ""); got == nil || got.ID != "t1" || got.SignIn != "claude-code" {
		t.Fatalf("NeedsSignIn = %+v", got)
	}
	if got := s.LastMessageTo("s1", "t1"); got != "Hi" {
		t.Fatalf("LastMessageTo = %q", got)
	}
	s.SignedIn("claude-code")
	if got := s.NeedsSignIn("s1", "t1"); got != nil {
		t.Fatalf("still needs sign-in: %+v", got)
	}
}

func TestReopeningATaskClearsItsSignIn(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	feed(t, s, `{"type":"task_created","session_id":"s1","seq":2,"task_id":"t1","description":"d","lane":"write","scope":[]}`)
	feed(t, s, `{"session_id":"s1","seq":3,"type":"model_error","task_id":"t1","model_key":"codex","kind":"auth","message":"x"}`)
	feed(t, s, `{"session_id":"s1","seq":4,"type":"task_reopened","task_id":"t1","run":2}`)
	if got := s.NeedsSignIn("s1", "t1"); got != nil {
		t.Fatalf("NeedsSignIn after reopen = %+v", got)
	}
}
