package sidebar

import (
	"strings"
	"testing"
	"time"

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

func TestDeleteArchiveAndCleanKeys(t *testing.T) {
	m := model(t, store(t))
	m.cursor = 1
	if msg, ok := run(m.Key(key("d"))).(DeleteMsg); !ok || msg.SessionID != "a1" || msg.Label == "" {
		t.Fatalf("d on a chat = %#v", msg)
	}
	if msg, ok := run(m.Key(key("a"))).(ArchiveMsg); !ok || msg.SessionID != "a1" || !msg.Archived {
		t.Fatalf("a on a chat = %#v", msg)
	}
	m.cursor = 0
	if msg, ok := run(m.Key(key("c"))).(CleanMsg); !ok || msg.Name != "shop" {
		t.Fatalf("c on a project = %#v", msg)
	}
	if run(m.Key(key("d"))) != nil {
		t.Fatal("d on a project row should do nothing")
	}
	if _, ok := run(m.Key(key("A"))).(ToggleArchivedMsg); !ok {
		t.Fatal("A should toggle archived chats")
	}
}

func TestFitCutsALabelToItsWidth(t *testing.T) {
	cases := []struct {
		in    string
		width int
		want  string
	}{
		{"short", 10, "short"},
		{"hello world", 8, "hello w…"},
		{"hello", 1, "…"},
	}
	for _, c := range cases {
		if got := fit(c.in, c.width); got != c.want {
			t.Errorf("fit(%q, %d) = %q, want %q", c.in, c.width, got, c.want)
		}
	}
	wide := fit("日本語のテキスト", 5)
	if ansi.StringWidth(wide) > 5 || !strings.HasSuffix(wide, "…") {
		t.Errorf("fit on wide characters = %q (width %d), want at most 5 columns ending in …", wide, ansi.StringWidth(wide))
	}
}

func TestFitIsFastOnAHugeLabel(t *testing.T) {
	huge := strings.Repeat("a", 100_000)
	start := time.Now()
	got := fit(huge, 30)
	if took := time.Since(start); took > 50*time.Millisecond {
		t.Fatalf("fit took %v on a 100,000-character label", took)
	}
	if ansi.StringWidth(got) != 30 || !strings.HasSuffix(got, "…") {
		t.Fatalf("fit returned %d columns, want 30 ending in …", ansi.StringWidth(got))
	}
}

func TestATaskRowShowsOnlyTheFirstLineOfItsDescription(t *testing.T) {
	task := &state.Task{ID: "t1", Description: "\n  Explain AQR HNSW.\nCONTEXT: a very long brief…"}
	if got := taskLabel(task); got != "Explain AQR HNSW." {
		t.Fatalf("taskLabel = %q", got)
	}
	if got := taskLabel(&state.Task{ID: "t2"}); got != "t2" {
		t.Fatalf("taskLabel with no description = %q", got)
	}
}
