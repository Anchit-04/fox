package app

import (
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"

	"fox/tui/internal/client"
	"fox/tui/internal/protocol"
	"fox/tui/internal/state"
	"fox/tui/internal/ui/composer"
	"fox/tui/internal/ui/sidebar"
	"fox/tui/internal/ui/theme"
)

type recorder struct {
	sent []protocol.Command
}

func (r *recorder) SendCmd(cmd protocol.Command) tea.Cmd {
	r.sent = append(r.sent, cmd)
	return nil
}

func (r *recorder) starts() []protocol.StartSession {
	var out []protocol.StartSession
	for _, c := range r.sent {
		if s, ok := c.(protocol.StartSession); ok {
			out = append(out, s)
		}
	}
	return out
}

func newApp(t *testing.T) (*Model, *recorder) {
	t.Helper()
	r := &recorder{}
	m := New(r, state.New(), theme.New("", ""), Options{})
	m.update(tea.WindowSizeMsg{Width: 120, Height: 40})
	m.sync()
	return &m, r
}

func event(t *testing.T, m *Model, frame string) {
	t.Helper()
	ev, err := protocol.Decode([]byte(frame))
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	m.update(client.EventMsg{Event: ev})
	m.sync()
}

func TestANewChatInAProjectWithSharedMemory(t *testing.T) {
	m, r := newApp(t)
	m.update(sidebar.NewChatMsg{Workspace: "/w/api", Name: "api"})
	m.update(tea.KeyPressMsg{Code: 's', Mod: tea.ModCtrl})
	m.sync()
	if !m.chat.shared {
		t.Fatal("ctrl+s did not turn on shared memory")
	}
	m.update(composer.SubmitMsg{Text: "add a route"})
	starts := r.starts()
	if len(starts) != 1 || starts[0].Workspace != "/w/api" || !starts[0].SharedMemory {
		t.Fatalf("starts = %+v", starts)
	}
	event(t, m, `{"type":"session_started","session_id":"s1","workspace":"/w/api","shared_memory":true}`)
	if m.active != "s1" || m.chat.shared {
		t.Fatalf("active %q, chat %+v", m.active, m.chat)
	}
}

func TestANewProjectAsksForAFolderThenStartsThere(t *testing.T) {
	m, r := newApp(t)
	m.update(sidebar.NewProjectMsg{})
	if !m.askPath {
		t.Fatal("new project did not ask for a folder")
	}
	m.update(composer.SubmitMsg{Text: `"/w/new"`})
	if m.askPath || m.chat.workspace != "/w/new" || m.chat.name != "new" {
		t.Fatalf("chat = %+v askPath = %v", m.chat, m.askPath)
	}
	if len(r.starts()) != 0 {
		t.Fatal("the folder path was sent as a task")
	}
	m.update(composer.SubmitMsg{Text: "set it up"})
	starts := r.starts()
	if len(starts) != 1 || starts[0].Workspace != "/w/new" || starts[0].SharedMemory {
		t.Fatalf("starts = %+v", starts)
	}
}

func TestAHubErrorOnStartIsFlashedAndKeepsTheTask(t *testing.T) {
	m, _ := newApp(t)
	m.update(sidebar.NewChatMsg{Workspace: "/w/missing", Name: "missing"})
	m.update(composer.SubmitMsg{Text: "do it"})
	event(t, m, `{"type":"error","message":"Workspace does not exist: /w/missing"}`)
	if m.flash != "Workspace does not exist: /w/missing" || m.starting != 0 {
		t.Fatalf("flash %q starting %d", m.flash, m.starting)
	}
	if m.composer.Value() != "do it" {
		t.Fatalf("composer = %q", m.composer.Value())
	}
}

func TestMemoryCannotBeSwitchedAfterStart(t *testing.T) {
	m, _ := newApp(t)
	event(t, m, `{"session_id":"s1","seq":1,"type":"session_started","task":"t","workspace":"/w/api","memory_space":"s1"}`)
	m.active = "s1"
	m.toggleShared()
	if m.chat.shared {
		t.Fatal("a running chat switched to shared memory")
	}
	if m.banner() != "" {
		t.Fatal("a running chat shows the new chat banner")
	}
}

func TestCtrlNStartsANewChatInTheActiveChatsProject(t *testing.T) {
	m, _ := newApp(t)
	event(t, m, `{"session_id":"s1","seq":1,"type":"session_started","task":"t","workspace":"/w/api","memory_space":"s1"}`)
	m.active = "s1"
	m.update(tea.KeyPressMsg{Code: 'n', Mod: tea.ModCtrl})
	if m.active != "" || m.chat.workspace != "/w/api" || m.chat.name != "api" {
		t.Fatalf("active %q chat %+v", m.active, m.chat)
	}
}

func startHandoff(t *testing.T, m *Model) {
	t.Helper()
	event(t, m, `{"type":"session_started","session_id":"s1","workspace":"/w/shop"}`)
	m.active = "s1"
	event(t, m, `{"type":"approval_requested","session_id":"s1","task_id":"t2","request_id":"handoff_1","kind":"handoff","tool_name":"browser_ask_human","args":{"url":"https://github.com/login"},"description":"sign in on github.com"}`)
}

func TestAHandoffIsAStatusLineNotABox(t *testing.T) {
	m, _ := newApp(t)
	startHandoff(t, m)
	if _, _, modal := m.pendingApproval(); modal {
		t.Fatal("a handoff must not open the approval box")
	}
	line := m.statusLine()
	for _, want := range []string{"waiting for you in the browser", "exec t2", "sign in on github.com", "ctrl+g skip"} {
		if !strings.Contains(line, want) {
			t.Errorf("status line %q is missing %q", line, want)
		}
	}
}

func TestCtrlGSkipsTheHandoff(t *testing.T) {
	m, r := newApp(t)
	startHandoff(t, m)
	m.update(tea.KeyPressMsg{Code: 'g', Mod: tea.ModCtrl})
	var got *protocol.ApprovalResponse
	for _, c := range r.sent {
		if a, ok := c.(protocol.ApprovalResponse); ok {
			got = &a
		}
	}
	if got == nil || got.RequestID != "handoff_1" || got.Approved || got.Always {
		t.Fatalf("ctrl+g sent %+v", got)
	}
	if _, ok := m.pendingHandoff(); ok {
		t.Fatal("the handoff is still pending after ctrl+g")
	}
}

func TestTheHandoffLineGoesWhenTheAgentCarriesOn(t *testing.T) {
	m, _ := newApp(t)
	startHandoff(t, m)
	event(t, m, `{"type":"tool_result","session_id":"s1","task_id":"t2","name":"browser_type","content":"The human finished the step."}`)
	if _, ok := m.pendingHandoff(); ok {
		t.Fatal("the handoff line should clear once the tool result arrives")
	}
}

func TestOrdinaryApprovalsStillUseTheBox(t *testing.T) {
	m, _ := newApp(t)
	event(t, m, `{"type":"session_started","session_id":"s1","workspace":"/w/shop"}`)
	m.active = "s1"
	event(t, m, `{"type":"approval_requested","session_id":"s1","request_id":"approval_1","tool_name":"run_bash_command","args":{"command":"curl x"},"description":"run: curl x"}`)
	if _, _, modal := m.pendingApproval(); !modal {
		t.Fatal("an ordinary approval should still open the box")
	}
	if _, ok := m.pendingHandoff(); ok {
		t.Fatal("an ordinary approval is not a handoff")
	}
}

func humanMessages(r *recorder) []protocol.HumanMessage {
	var out []protocol.HumanMessage
	for _, c := range r.sent {
		if h, ok := c.(protocol.HumanMessage); ok {
			out = append(out, h)
		}
	}
	return out
}

func TestAMessageToAnInterruptedChatIsSentSoItCanResume(t *testing.T) {
	m, r := newApp(t)
	event(t, m, `{"type":"session_started","session_id":"s1","workspace":"/w/shop"}`)
	event(t, m, `{"type":"session_ended","session_id":"s1","seq":9,"status":"interrupted","final_text":"interrupted: server restarted"}`)
	m.active = "s1"
	if !strings.Contains(m.placeholder(), "pick this chat up again") {
		t.Fatalf("placeholder = %q", m.placeholder())
	}
	m.update(composer.SubmitMsg{Text: "carry on"})
	sent := humanMessages(r)
	if len(sent) != 1 || sent[0].SessionID != "s1" || sent[0].Content != "carry on" {
		t.Fatalf("sent = %+v", sent)
	}
}

func TestAFailedChatStillRefusesMessages(t *testing.T) {
	m, r := newApp(t)
	event(t, m, `{"type":"session_started","session_id":"s1","workspace":"/w/shop"}`)
	event(t, m, `{"type":"session_ended","session_id":"s1","seq":9,"status":"failed","final_text":"worker failed to start"}`)
	m.active = "s1"
	m.update(composer.SubmitMsg{Text: "hello?"})
	if len(humanMessages(r)) != 0 {
		t.Fatal("a failed chat should not be messaged")
	}
}

func TestAFinishedExecutorCanBeMessaged(t *testing.T) {
	m, r := newApp(t)
	event(t, m, `{"type":"session_started","session_id":"s1","workspace":"/w/shop","mode":"orchestrator"}`)
	event(t, m, `{"type":"task_created","session_id":"s1","seq":2,"task_id":"t1","description":"look","lane":"read","scope":[]}`)
	event(t, m, `{"type":"task_status_changed","session_id":"s1","seq":3,"task_id":"t1","status":"done","report":{"outcome":"ok","changes":[],"validation":"v","blockers":[]}}`)
	m.active, m.activeTask = "s1", "t1"
	m.update(composer.SubmitMsg{Text: "also the empty cart"})
	sent := humanMessages(r)
	if len(sent) != 1 || sent[0].TaskID == nil || *sent[0].TaskID != "t1" {
		t.Fatalf("sent = %+v", sent)
	}
}

func sentKinds(r *recorder) []string {
	var kinds []string
	for _, c := range r.sent {
		kinds = append(kinds, c.Kind())
	}
	return kinds
}

func TestDeletingAChatAsksTwiceThenSends(t *testing.T) {
	m, r := newApp(t)
	event(t, m, `{"type":"session_started","session_id":"s1","workspace":"/w/shop","task":"old chat"}`)
	m.update(sidebar.DeleteMsg{SessionID: "s1", Label: "old chat"})
	if !strings.Contains(m.flash, "press d again") {
		t.Fatalf("flash = %q", m.flash)
	}
	for _, k := range sentKinds(r) {
		if k == "delete_session" {
			t.Fatal("deleted without asking")
		}
	}
	m.update(sidebar.DeleteMsg{SessionID: "s1", Label: "old chat"})
	last := r.sent[len(r.sent)-1]
	if d, ok := last.(protocol.DeleteSession); !ok || d.SessionID != "s1" {
		t.Fatalf("last sent = %+v", last)
	}
}

func TestARemovedChatLeavesTheSidebarAndTheView(t *testing.T) {
	m, _ := newApp(t)
	event(t, m, `{"type":"session_started","session_id":"s1","workspace":"/w/shop"}`)
	m.active = "s1"
	event(t, m, `{"type":"session_removed","session_ids":["s1"],"reason":"deleted"}`)
	if m.store.Session("s1") != nil || m.active != "" {
		t.Fatalf("session still there, active %q", m.active)
	}
	if len(m.store.Projects()) != 0 {
		t.Fatal("the empty project should be gone too")
	}
}

func TestCleaningAProjectPreviewsTheCountFirst(t *testing.T) {
	m, r := newApp(t)
	m.update(sidebar.CleanMsg{Workspace: "/w/shop", Name: "shop"})
	first := r.sent[len(r.sent)-1].(protocol.CleanSessions)
	if !first.DryRun || first.Project != "/w/shop" {
		t.Fatalf("first clean = %+v", first)
	}
	event(t, m, `{"type":"clean_sessions_preview","project":"/w/shop","count":18,"session_ids":[]}`)
	if !strings.Contains(m.flash, "delete 18 stopped chats") {
		t.Fatalf("flash = %q", m.flash)
	}
	m.update(sidebar.CleanMsg{Workspace: "/w/shop", Name: "shop"})
	second := r.sent[len(r.sent)-1].(protocol.CleanSessions)
	if second.DryRun {
		t.Fatal("the second c should really delete")
	}
}

func TestArchivedChatsHideUntilShown(t *testing.T) {
	m, r := newApp(t)
	event(t, m, `{"type":"sessions","sessions":[{"session_id":"s1","workspace":"/w/shop","task":"t","status":"done","archived":true},{"session_id":"s2","workspace":"/w/shop","task":"t","status":"done"}]}`)
	if got := len(m.store.Projects()[0].Sessions); got != 1 {
		t.Fatalf("visible sessions = %d", got)
	}
	m.update(sidebar.ToggleArchivedMsg{})
	if got := len(m.store.Projects()[0].Sessions); got != 2 {
		t.Fatalf("with archived shown = %d", got)
	}
	if l, ok := r.sent[len(r.sent)-1].(protocol.ListSessions); !ok || !l.IncludeArchived {
		t.Fatalf("last sent = %+v", r.sent[len(r.sent)-1])
	}
}

func userEntries(sess *state.Session) []string {
	var texts []string
	for _, e := range sess.Entries {
		if e.Kind == state.UserEntry {
			texts = append(texts, e.Text)
		}
	}
	return texts
}

func TestTheFirstMessageOfANewChatShowsOnce(t *testing.T) {
	m, _ := newApp(t)
	m.update(composer.SubmitMsg{Text: "Hi"})
	event(t, m, `{"type":"session_started","session_id":"s1","workspace":"/w/shop","shared_memory":false}`)
	event(t, m, `{"type":"session_started","session_id":"s1","seq":1,"task":"Hi","workspace":"/w/shop","mode":"agent","permission_mode":"workspace-write"}`)
	if got := userEntries(m.store.Session("s1")); len(got) != 1 || got[0] != "Hi" {
		t.Fatalf("user entries = %q", got)
	}
}

func TestReplyMessagesNeverCreateGhostChats(t *testing.T) {
	m, _ := newApp(t)
	event(t, m, `{"type":"session_started","session_id":"s1","seq":1,"task":"old","workspace":"/w/shop"}`)
	event(t, m, `{"type":"session_removed","session_ids":["s1"],"reason":"deleted"}`)
	for _, frame := range []string{
		`{"type":"delete_session_done","session_id":"s1","session_ids":["s1"]}`,
		`{"type":"kill_session_done","session_id":"s1"}`,
		`{"type":"human_message_queued","session_id":"s1","task_id":null}`,
		`{"type":"end_session_queued","session_id":"s1"}`,
		`{"type":"clean_sessions_done","project":"/w/shop","count":1,"session_ids":["s1"]}`,
	} {
		event(t, m, frame)
	}
	if m.store.Session("s1") != nil || len(m.store.Projects()) != 0 {
		t.Fatalf("a ghost chat appeared: %+v", m.store.Projects())
	}
}
