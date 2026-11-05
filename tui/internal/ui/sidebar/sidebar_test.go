package sidebar

import (
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"

	"fox/tui/internal/protocol"
	"fox/tui/internal/state"
	"fox/tui/internal/ui/theme"
)

func store(t *testing.T) *state.Store {
	t.Helper()
	s := state.New()
	for _, frame := range []string{
		`{"session_id":"a1","seq":1,"type":"session_started","task":"fix the cart","workspace":"/w/shop","memory_space":"project:/w/shop"}`,
		`{"session_id":"b1","seq":1,"type":"session_started","task":"add a route","workspace":"/w/api","memory_space":"b1"}`,
	} {
		ev, err := protocol.Decode([]byte(frame))
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		s.Apply(ev)
	}
	return s
}

func model(t *testing.T, s *state.Store) Model {
	t.Helper()
	m := New(theme.New("", ""))
	m.SetSize(40, 30)
	m.Refresh(s, "", "")
	return m
}

func run(cmd tea.Cmd) tea.Msg {
	if cmd == nil {
		return nil
	}
	return cmd()
}

func key(text string) tea.KeyPressMsg {
	return tea.KeyPressMsg{Code: rune(text[0]), Text: text}
}

func TestRowsHaveNoNewSessionRowAndKnowTheirProject(t *testing.T) {
	m := model(t, store(t))
	if len(m.rows) != 4 {
		t.Fatalf("rows = %d", len(m.rows))
	}
	for i, r := range m.rows {
		if r.project.Name == "" {
			t.Fatalf("row %d has no project", i)
		}
	}
	if m.rows[1].project.Name != "shop" || m.rows[3].project.Name != "api" {
		t.Fatalf("projects = %q %q", m.rows[1].project.Name, m.rows[3].project.Name)
	}
}

func TestNKeyStartsAChatInTheFocusedProject(t *testing.T) {
	m := model(t, store(t))
	m.cursor = 2
	got, ok := run(m.Key(key("n"))).(NewChatMsg)
	if !ok || got.Workspace != "/w/api" || got.Name != "api" {
		t.Fatalf("msg = %#v", got)
	}
	if _, ok := run(m.Key(key("p"))).(NewProjectMsg); !ok {
		t.Fatal("p did not ask for a new project")
	}
}

func TestPlusOnAProjectRowStartsAChatThere(t *testing.T) {
	m := model(t, store(t))
	got, ok := run(m.Press(m.width-1, listTop+2, tea.MouseLeft)).(NewChatMsg)
	if !ok || got.Workspace != "/w/api" {
		t.Fatalf("msg = %#v", got)
	}
	if !strings.HasSuffix(strings.TrimRight(stripped(m.renderRow(m.rows[0], false)), " "), "+") {
		t.Fatalf("project row has no +: %q", stripped(m.renderRow(m.rows[0], false)))
	}
}

func TestTopActionsSitSideBySide(t *testing.T) {
	m := model(t, store(t))
	m.cursor = 1
	if got, ok := run(m.Press(2, actionsRow, tea.MouseLeft)).(NewChatMsg); !ok || got.Name != "shop" {
		t.Fatalf("new chat msg = %#v", got)
	}
	x := len(newChatLabel) + 3
	if _, ok := run(m.Press(x, actionsRow, tea.MouseLeft)).(NewProjectMsg); !ok {
		t.Fatal("new project was not clicked")
	}
	top := strings.Split(stripped(m.View(false)), "\n")[actionsRow]
	if !strings.Contains(top, newChatLabel) || !strings.Contains(top, newProjLabel) {
		t.Fatalf("top line = %q", top)
	}
	if strings.Contains(stripped(m.View(false)), "new session") {
		t.Fatal("the old new session row is still drawn")
	}
}

func TestSharedChatsCarryAMark(t *testing.T) {
	m := model(t, store(t))
	shared, private := stripped(m.renderRow(m.rows[1], false)), stripped(m.renderRow(m.rows[3], false))
	if !strings.Contains(shared, sharedMark) || strings.Contains(private, sharedMark) {
		t.Fatalf("shared %q, private %q", shared, private)
	}
}

func stripped(s string) string {
	return ansi.Strip(s)
}
