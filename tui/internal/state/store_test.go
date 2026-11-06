package state

import (
	"encoding/json"
	"fmt"
	"runtime"
	"slices"
	"strings"
	"testing"

	"fox/tui/internal/protocol"
)

func feed(t *testing.T, s *Store, frame string) {
	t.Helper()
	ev, err := protocol.Decode([]byte(frame))
	if err != nil {
		t.Fatalf("Decode %s: %v", frame, err)
	}
	s.Apply(ev)
}

func start(t *testing.T, s *Store, id, workspace string) {
	t.Helper()
	feed(t, s, fmt.Sprintf(`{"session_id":%q,"type":"session_started","task":"task %s","workspace":%q}`, id, id, workspace))
}

func turn(seq int, typ, taskID, payload string) protocol.Stored {
	st := protocol.Stored{Seq: seq, Type: typ, Payload: json.RawMessage(payload)}
	if taskID != "" {
		st.TaskID = &taskID
	}
	return st
}

func attach(t *testing.T, s *Store, id string, view protocol.SessionView) {
	t.Helper()
	data, err := json.Marshal(map[string]any{"session_id": id, "type": protocol.Attached, "state": view})
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	feed(t, s, string(data))
}

func ids(sessions []*Session) []string {
	var out []string
	for _, sess := range sessions {
		out = append(out, sess.ID)
	}
	return out
}

func TestStatusOfAnotherSessionAddsAStub(t *testing.T) {
	s := New()
	feed(t, s, `{"type":"session_status","session_id":"elsewhere","workspace":"C:/api","task":"fix login","status":"running","mode":"agent"}`)
	feed(t, s, `{"type":"projects","current":"C:/shop","projects":[]}`)
	sess := s.Session("elsewhere")
	if sess == nil || len(s.Order) != 1 {
		t.Fatalf("got sessions %v", s.Order)
	}
	if sess.Loaded || len(sess.Entries) != 0 || sess.Status != Working || sess.Task != "fix login" || sess.Workspace != "C:/api" {
		t.Fatalf("stub = %+v", sess)
	}
	feed(t, s, `{"type":"session_status","session_id":"elsewhere","workspace":"C:/api","task":"fix login","status":"idle"}`)
	if sess.Status != Idle {
		t.Fatalf("status = %v", sess.Status)
	}
}

func TestStatusLeavesALoadedSessionAlone(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	feed(t, s, `{"type":"session_status","session_id":"s1","workspace":"/w/other","task":"other","status":"done"}`)
	sess := s.Session("s1")
	if !sess.Loaded || sess.Status != Working || sess.Task != "task s1" || sess.Workspace != "/w/fox" {
		t.Fatalf("session = %+v", sess)
	}
}

func TestAttachedFillsAStub(t *testing.T) {
	s := New()
	feed(t, s, `{"type":"session_status","session_id":"s1","workspace":"/w/fox","task":"hi","status":"idle"}`)
	attach(t, s, "s1", protocol.SessionView{
		Task: "hi", Workspace: "/w/fox", Mode: "agent", Status: "running",
		Turns: []protocol.Stored{
			turn(1, protocol.SessionStarted, "", `{"task":"hi","workspace":"/w/fox","mode":"agent"}`),
			turn(2, protocol.AgentTurn, "", `{"model_key":"m","text":"hello"}`),
		},
	})
	sess := s.Session("s1")
	if !sess.Loaded || len(sess.Entries) != 2 || len(s.Order) != 1 {
		t.Fatalf("session = %+v, order %v", sess, s.Order)
	}
}

func TestListingAddsStubsOldestFirst(t *testing.T) {
	s := New()
	start(t, s, "mine", "/w/fox")
	feed(t, s, `{"type":"sessions","sessions":[
		{"session_id":"mine","workspace":"/w/fox","task":"renamed","status":"done"},
		{"session_id":"side","parent_id":"old","workspace":"/w/fox","task":"[Side conversation. ctx]\nQuestion: why?","status":"done"},
		{"session_id":"old","workspace":"/w/shop","task":"older","status":"failed"}]}`)
	if !slices.Equal(s.Order, []string{"old", "side", "mine"}) {
		t.Fatalf("order = %v", s.Order)
	}
	if mine := s.Session("mine"); mine.Task != "task mine" || len(mine.Entries) != 1 {
		t.Fatalf("loaded session changed: %+v", mine)
	}
	if old := s.Session("old"); old.Loaded || old.Status != Failed {
		t.Fatalf("old = %+v", old)
	}
	if side := s.Session("side"); side.ParentID != "old" || side.Task != "why?" {
		t.Fatalf("side = %+v", side)
	}
	if got := ids(s.Children("old")); !slices.Equal(got, []string{"side"}) {
		t.Fatalf("children = %v", got)
	}
}

func TestProjectsMergeSpellingsOfOneFolder(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("paths are case-sensitive here")
	}
	s := New()
	start(t, s, "s1", `C:\Users\me\Desktop\Fox`)
	start(t, s, "s2", `c:\users\me\desktop\fox\`)
	if projects := s.Projects(); len(projects) != 1 || len(projects[0].Sessions) != 2 {
		t.Fatalf("projects = %+v", projects)
	}
}

func TestStartedTakesPendingStart(t *testing.T) {
	s := New()
	s.ExpectStart(PendingStart{Task: "fix the login bug", Mode: "build"})
	feed(t, s, `{"session_id":"s1","type":"session_started"}`)

	sess := s.Session("s1")
	if sess.Task != "fix the login bug" || sess.Mode != "build" || sess.Status != Working {
		t.Fatalf("session = %+v", sess)
	}
	if len(sess.Entries) != 1 || sess.Entries[0].Kind != UserEntry || sess.Entries[0].Text != "fix the login bug" {
		t.Fatalf("entries = %+v", sess.Entries)
	}
	if len(s.pending) != 0 {
		t.Fatalf("pending start not consumed: %+v", s.pending)
	}
}

func TestBroadcastStartLeavesPendingAlone(t *testing.T) {
	s := New()
	s.ExpectStart(PendingStart{Task: "mine"})
	start(t, s, "other", "/w/shop")

	if got := s.Session("other").Task; got != "task other" {
		t.Fatalf("task = %q", got)
	}
	if len(s.pending) != 1 {
		t.Fatalf("pending start was stolen: %+v", s.pending)
	}
}

func TestInjectedConfirmsPendingEcho(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	s.AddUserMessage("s1", "", "add tests")
	feed(t, s, `{"session_id":"s1","type":"human_message_injected","task_id":null,"content":"add tests\n"}`)

	entries := s.Session("s1").Entries
	if len(entries) != 2 || entries[1].Pending {
		t.Fatalf("entries = %+v", entries)
	}
}

func TestInjectedWithoutEchoAppends(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	feed(t, s, `{"session_id":"s1","type":"human_message_injected","task_id":null,"content":"from another window"}`)

	entries := s.Session("s1").Entries
	if len(entries) != 2 || entries[1].Kind != UserEntry || entries[1].Text != "from another window" || entries[1].Pending {
		t.Fatalf("entries = %+v", entries)
	}
}

func TestNeedsYouIsSticky(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	feed(t, s, `{"session_id":"s1","type":"approval_requested","task_id":"t1","request_id":"approval_1","tool_name":"write_file"}`)
	feed(t, s, `{"session_id":"s1","type":"tool_call","task_id":"t2","name":"read_file","args":{"path":"a.py"}}`)
	feed(t, s, `{"session_id":"s1","type":"agent_turn","task_id":null,"model_key":"nemotron-super","text":"waiting on t1"}`)

	if got := s.Session("s1").Status; got != NeedsYou {
		t.Fatalf("status = %v, want NeedsYou", got)
	}
}

func TestResolveApprovalMatchesSession(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	start(t, s, "s2", "/w/shop")
	feed(t, s, `{"session_id":"s1","type":"approval_requested","task_id":null,"request_id":"approval_1","tool_name":"run_shell"}`)
	feed(t, s, `{"session_id":"s2","type":"approval_requested","task_id":null,"request_id":"approval_1","tool_name":"write_file"}`)

	a, ok := s.ResolveApproval("s2", "approval_1")
	if !ok || a.SessionID != "s2" || a.ToolName != "write_file" {
		t.Fatalf("resolved = %+v, ok = %v", a, ok)
	}
	if s.Session("s2").Status != Working || s.Session("s1").Status != NeedsYou {
		t.Fatalf("s1 = %v, s2 = %v", s.Session("s1").Status, s.Session("s2").Status)
	}
	if len(s.Approvals) != 1 || s.Approvals[0].SessionID != "s1" {
		t.Fatalf("approvals = %+v", s.Approvals)
	}
	if _, ok := s.ResolveApproval("s2", "approval_1"); ok {
		t.Fatal("resolved the same approval twice")
	}
}

func TestToolResultSettlesItsOwnTask(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	feed(t, s, `{"session_id":"s1","type":"approval_requested","task_id":"t1","request_id":"approval_1","tool_name":"write_file"}`)
	feed(t, s, `{"session_id":"s1","type":"approval_requested","task_id":"t2","request_id":"approval_2","tool_name":"run_shell"}`)

	feed(t, s, `{"session_id":"s1","type":"tool_result","task_id":"t1","name":"write_file","content":"ok"}`)
	if len(s.Approvals) != 1 || s.Approvals[0].TaskID != "t2" || s.Session("s1").Status != NeedsYou {
		t.Fatalf("after t1: approvals = %+v, status = %v", s.Approvals, s.Session("s1").Status)
	}

	feed(t, s, `{"session_id":"s1","type":"tool_denied","task_id":"t2","name":"run_shell","reason":"denied"}`)
	if len(s.Approvals) != 0 || s.Session("s1").Status != Working {
		t.Fatalf("after t2: approvals = %+v, status = %v", s.Approvals, s.Session("s1").Status)
	}
}

func TestSessionEndedClearsApprovals(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	feed(t, s, `{"session_id":"s1","type":"approval_requested","task_id":null,"request_id":"approval_1","tool_name":"run_shell"}`)
	feed(t, s, `{"session_id":"s1","type":"session_ended","status":"failed","final_text":"boom"}`)

	sess := s.Session("s1")
	if len(s.Approvals) != 0 || sess.Status != Failed {
		t.Fatalf("approvals = %+v, status = %v", s.Approvals, sess.Status)
	}
	last := sess.Entries[len(sess.Entries)-1]
	if last.Kind != ErrorEntry || last.Text != "session failed: boom" {
		t.Fatalf("last entry = %+v", last)
	}

	start(t, s, "s2", "/w/fox")
	feed(t, s, `{"session_id":"s2","type":"session_ended","status":"done","final_text":"all good"}`)
	if got := s.Session("s2").Status; got != Done {
		t.Fatalf("s2 status = %v, want Done", got)
	}
}

func TestExecutorTurnKeepsSessionWorking(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	feed(t, s, `{"session_id":"s1","type":"agent_turn","task_id":"t1","model_key":"gemini-lite","text":"on it"}`)

	sess := s.Session("s1")
	if sess.Status != Working || sess.Model != "" || sess.Tasks["t1"].Model != "gemini-lite" {
		t.Fatalf("status = %v, model = %q, task model = %q", sess.Status, sess.Model, sess.Tasks["t1"].Model)
	}

	feed(t, s, `{"session_id":"s1","type":"agent_turn","task_id":null,"model_key":"nemotron-super","text":"done, anything else?"}`)
	if sess.Status != Idle || sess.Model != "nemotron-super" {
		t.Fatalf("status = %v, model = %q", sess.Status, sess.Model)
	}
}

func TestCostCountsUnknownPriceAsPartial(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	feed(t, s, `{"session_id":"s1","type":"model_called","task_id":null,"input_tokens":100,"output_tokens":10,"cost":0.25}`)
	feed(t, s, `{"session_id":"s1","type":"model_called","task_id":"t1","input_tokens":200,"output_tokens":20,"cost":0.5}`)
	feed(t, s, `{"session_id":"s1","type":"model_called","task_id":null,"input_tokens":50,"output_tokens":5,"cost":null}`)

	sess := s.Session("s1")
	if sess.Cost != 0.75 || !sess.CostPartial || sess.Tasks["t1"].Cost != 0.5 {
		t.Fatalf("cost = %v, partial = %v, t1 = %v", sess.Cost, sess.CostPartial, sess.Tasks["t1"].Cost)
	}
	if sess.InputTokens != 350 || sess.OutputTokens != 35 {
		t.Fatalf("tokens = %d in, %d out", sess.InputTokens, sess.OutputTokens)
	}
}

func TestFreeModelIsNotPartial(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	feed(t, s, `{"session_id":"s1","type":"model_called","task_id":null,"input_tokens":100,"output_tokens":10,"cost":0}`)

	if sess := s.Session("s1"); sess.Cost != 0 || sess.CostPartial {
		t.Fatalf("cost = %v, partial = %v", sess.Cost, sess.CostPartial)
	}
}

func TestAttachReplayKeepsOnlyOpenApprovals(t *testing.T) {
	s := New()
	attach(t, s, "s1", protocol.SessionView{
		Task:      "refactor",
		Workspace: "/w/fox",
		Status:    "running",
		Turns: []protocol.Stored{
			turn(1, protocol.SessionStarted, "", `{"task":"refactor","workspace":"/w/fox"}`),
			turn(2, protocol.ApprovalRequested, "", `{"request_id":"approval_1","tool_name":"run_shell"}`),
			turn(3, protocol.ToolResult, "", `{"name":"run_shell","content":"ok"}`),
			turn(4, protocol.ApprovalRequested, "", `{"request_id":"approval_2","tool_name":"write_file"}`),
		},
	})

	sess := s.Session("s1")
	if len(s.Approvals) != 1 || s.Approvals[0].RequestID != "approval_2" {
		t.Fatalf("approvals = %+v", s.Approvals)
	}
	if sess.Status != NeedsYou || sess.Unread {
		t.Fatalf("status = %v, unread = %v", sess.Status, sess.Unread)
	}
}

func TestReattachSkipsSeenTurns(t *testing.T) {
	s := New()
	view := protocol.SessionView{
		Task:      "refactor",
		Workspace: "/w/fox",
		Status:    "running",
		Turns: []protocol.Stored{
			turn(1, protocol.SessionStarted, "", `{"task":"refactor","workspace":"/w/fox"}`),
			turn(2, protocol.AgentTurn, "", `{"model_key":"nemotron-super","text":"looking"}`),
		},
	}
	attach(t, s, "s1", view)
	attach(t, s, "s1", view)

	if got := len(s.Session("s1").Entries); got != 2 {
		t.Fatalf("entries = %d, want 2", got)
	}
	if len(s.Order) != 1 {
		t.Fatalf("order = %v", s.Order)
	}
}

func TestAttachedEndedSessionKeepsFinalStatus(t *testing.T) {
	s := New()
	attach(t, s, "s1", protocol.SessionView{
		Task:   "refactor",
		Status: "done",
		Turns: []protocol.Stored{
			turn(1, protocol.SessionStarted, "", `{"task":"refactor"}`),
			turn(2, protocol.AgentTurn, "", `{"model_key":"nemotron-super","text":"finished"}`),
		},
	})

	if got := s.Session("s1").Status; got != Done {
		t.Fatalf("status = %v, want Done", got)
	}
}

func TestProjectsGroupByWorkspace(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	start(t, s, "s2", "/w/shop")
	start(t, s, "s3", "/w/fox")
	feed(t, s, `{"session_id":"s4","type":"session_started","task":"side question","workspace":"/w/fox","parent_id":"s1"}`)
	start(t, s, "s5", "")

	projects := s.Projects()
	if len(projects) != 3 {
		t.Fatalf("projects = %+v", projects)
	}
	want := []struct {
		name string
		ids  []string
	}{
		{"fox", []string{"s1", "s3"}},
		{"shop", []string{"s2"}},
		{"this workspace", []string{"s5"}},
	}
	for i, w := range want {
		if projects[i].Name != w.name || !slices.Equal(ids(projects[i].Sessions), w.ids) {
			t.Errorf("project %d = %s %v, want %s %v", i, projects[i].Name, ids(projects[i].Sessions), w.name, w.ids)
		}
	}
	if got := ids(s.Children("s1")); !slices.Equal(got, []string{"s4"}) {
		t.Fatalf("children = %v", got)
	}
}

func TestSideThreadShowsOnlyTheQuestion(t *testing.T) {
	s := New()
	wrapped := "[Side conversation. The main task is still running.]\n\nMain task: refactor\n\nQuestion: why websockets?"
	attach(t, s, "s2", protocol.SessionView{
		Task:  wrapped,
		Turns: []protocol.Stored{turn(1, protocol.SessionStarted, "", fmt.Sprintf(`{"task":%q,"parent_id":"s1"}`, wrapped))},
	})

	sess := s.Session("s2")
	if sess.Task != "why websockets?" || sess.Entries[0].Text != "why websockets?" {
		t.Fatalf("task = %q, first entry = %q", sess.Task, sess.Entries[0].Text)
	}
}

func TestErrorWithoutSessionBecomesNotice(t *testing.T) {
	s := New()
	feed(t, s, `{"type":"error","message":"unknown model"}`)

	if !slices.Equal(s.Notices, []string{"unknown model"}) || len(s.Sessions) != 0 {
		t.Fatalf("notices = %v, sessions = %d", s.Notices, len(s.Sessions))
	}
}

func TestScopeAuditOnlyWhenMispredicted(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	feed(t, s, `{"session_id":"s1","type":"scope_audit","task_id":"t1","mispredicted":false,"undeclared":[]}`)
	feed(t, s, `{"session_id":"s1","type":"scope_audit","task_id":"t2","mispredicted":true,"undeclared":["a.py","b.py"]}`)

	entries := s.Session("s1").Entries
	if len(entries) != 2 {
		t.Fatalf("entries = %+v", entries)
	}
	if e := entries[1]; e.Kind != NoticeEntry || e.TaskID != "t2" || e.Text != "wrote outside its declared scope: a.py, b.py" {
		t.Fatalf("notice = %+v", e)
	}
}

func TestModelErrorPausesUntilAMessageArrives(t *testing.T) {
	s := New()
	start(t, s, "s1", "/w/fox")
	feed(t, s, `{"session_id":"s1","type":"model_error","task_id":null,"model_key":"nemotron-super","kind":"key","message":"Error code: 401"}`)
	sess := s.Session("s1")
	last := sess.Entries[len(sess.Entries)-1]
	if sess.Status != NeedsYou || last.Kind != ErrorEntry || !strings.Contains(last.Text, "vault") {
		t.Fatalf("status = %v, last = %+v", sess.Status, last)
	}
	feed(t, s, `{"session_id":"s1","type":"human_message_injected","task_id":null,"content":"retry"}`)
	if sess.Status != Working {
		t.Fatalf("after retry status = %v, want Working", sess.Status)
	}
}

func TestSharedMemoryFromEverySource(t *testing.T) {
	s := New()
	feed(t, s, `{"type":"session_status","session_id":"st","workspace":"/w/api","task":"t","status":"running","shared_memory":true}`)
	feed(t, s, `{"type":"sessions","sessions":[{"session_id":"ls","workspace":"/w/api","task":"t","status":"done","shared_memory":true},{"session_id":"lp","workspace":"/w/api","task":"t","status":"done","shared_memory":false}]}`)
	feed(t, s, `{"session_id":"ev","seq":1,"type":"session_started","task":"t","workspace":"/w/api","memory_space":"project:/w/api"}`)
	feed(t, s, `{"session_id":"pv","seq":1,"type":"session_started","task":"t","workspace":"/w/api","memory_space":"pv"}`)
	feed(t, s, `{"session_id":"rp","type":"session_started","workspace":"/w/api","shared_memory":true}`)
	space := "project:/w/api"
	attach(t, s, "at", protocol.SessionView{Task: "t", Workspace: "/w/api", Status: "done", MemorySpace: &space})
	for id, want := range map[string]bool{"st": true, "ls": true, "lp": false, "ev": true, "pv": false, "rp": true, "at": true} {
		if got := s.Session(id).SharedMemory; got != want {
			t.Errorf("%s: SharedMemory = %v, want %v", id, got, want)
		}
	}
}

func TestProjectsReplyRecordsTheDefaultWorkspace(t *testing.T) {
	s := New()
	feed(t, s, `{"type":"projects","current":"C:/work/shop","projects":[]}`)
	if s.DefaultWorkspace != "C:/work/shop" {
		t.Fatalf("DefaultWorkspace = %q", s.DefaultWorkspace)
	}
	if got := ProjectName(s.DefaultWorkspace); got != "shop" {
		t.Fatalf("ProjectName = %q", got)
	}
	if got := ProjectName(""); got != "this workspace" {
		t.Fatalf("ProjectName of nothing = %q", got)
	}
}

func TestDropStartForgetsAFailedStart(t *testing.T) {
	s := New()
	s.ExpectStart(PendingStart{Task: "in a folder that does not exist"})
	s.ExpectStart(PendingStart{Task: "in the api folder"})
	s.DropStart()
	feed(t, s, `{"session_id":"s1","type":"session_started","workspace":"/w/api"}`)
	if got := s.Session("s1").Task; got != "in the api folder" {
		t.Fatalf("task = %q", got)
	}
	s.DropStart()
	if len(s.pending) != 0 {
		t.Fatalf("pending = %+v", s.pending)
	}
}

func TestChatsInTwoProjectsGroupSeparately(t *testing.T) {
	s := New()
	start(t, s, "a1", "/w/shop")
	start(t, s, "b1", "/w/api")
	start(t, s, "a2", "/w/shop")
	projects := s.Projects()
	if len(projects) != 2 || projects[0].Name != "shop" || projects[1].Name != "api" {
		t.Fatalf("projects = %+v", projects)
	}
	if got := ids(projects[0].Sessions); !slices.Equal(got, []string{"a1", "a2"}) {
		t.Fatalf("shop chats = %v", got)
	}
}

func TestAnInterruptedChatIsQuietAndCanResume(t *testing.T) {
	s := New()
	feed(t, s, `{"type":"session_started","session_id":"s1","task":"fix","workspace":"/w"}`)
	feed(t, s, `{"type":"session_ended","session_id":"s1","seq":5,"status":"interrupted","final_text":"interrupted: server restarted"}`)
	sess := s.Session("s1")
	if sess.Status != Interrupted || !sess.Status.Stopped() {
		t.Fatalf("status = %v", sess.Status)
	}
	last := sess.Entries[len(sess.Entries)-1]
	if last.Kind != NoticeEntry || last.Text != "interrupted: server restarted · send a message to continue" {
		t.Fatalf("last entry = %+v", last)
	}
	feed(t, s, `{"type":"session_resumed","session_id":"s1","seq":6,"reason":"your message"}`)
	if sess.Status != Working {
		t.Fatalf("after resume status = %v", sess.Status)
	}
}

func TestPausedIsItsOwnStatus(t *testing.T) {
	s := New()
	feed(t, s, `{"type":"session_started","session_id":"s1","task":"fix","workspace":"/w"}`)
	feed(t, s, `{"type":"session_ended","session_id":"s1","seq":5,"status":"paused","final_text":"paused: idle"}`)
	if got := s.Session("s1").Status; got != Paused {
		t.Fatalf("status = %v", got)
	}
}

func TestAReopenedTaskRunsAgain(t *testing.T) {
	s := New()
	feed(t, s, `{"type":"session_started","session_id":"s1","task":"fix","workspace":"/w"}`)
	feed(t, s, `{"type":"task_created","session_id":"s1","seq":2,"task_id":"t1","description":"d","lane":"read","scope":[]}`)
	feed(t, s, `{"type":"task_status_changed","session_id":"s1","seq":3,"task_id":"t1","status":"failed","error":"boom"}`)
	feed(t, s, `{"type":"task_reopened","session_id":"s1","seq":4,"task_id":"t1","run":2}`)
	task := s.Session("s1").Tasks["t1"]
	if task.Status != "running" || task.Error != "" {
		t.Fatalf("task = %+v", task)
	}
}
