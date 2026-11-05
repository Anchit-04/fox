package app

import (
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
