package sidebar

import (
	"fmt"
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
	if len(m.rows) != 6 {
		t.Fatalf("rows = %d", len(m.rows))
	}
	for i, r := range m.rows {
		if r.project.Name == "" {
			t.Fatalf("row %d has no project", i)
		}
	}
	if m.rows[1].project.Name != "shop" || m.rows[4].project.Name != "api" {
		t.Fatalf("projects = %q %q", m.rows[1].project.Name, m.rows[4].project.Name)
	}
}

func TestNKeyStartsAChatInTheFocusedProject(t *testing.T) {
	m := model(t, store(t))
	m.cursor = 3
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
	got, ok := run(m.Press(m.width-1, listTop+3, tea.MouseLeft)).(NewChatMsg)
	if !ok || got.Workspace != "/w/api" {
		t.Fatalf("msg = %#v", got)
	}
	if !strings.HasSuffix(strings.TrimRight(stripped(m.renderRow(m.rows[0], false)), " "), "+") {
		t.Fatalf("project row has no +: %q", stripped(m.renderRow(m.rows[0], false)))
	}
}

func TestTheTopRowIsTwoTabsAndAnAddButton(t *testing.T) {
	s := store(t)
	m := model(t, s)
	top := strings.Split(stripped(m.View(false)), "\n")[actionsRow]
	if !strings.Contains(top, "Projects") || !strings.Contains(top, "Chat") || strings.Contains(top, "new chat") {
		t.Fatalf("top line = %q", top)
	}
	if _, ok := run(m.Press(m.width-2, actionsRow, tea.MouseLeft)).(NewProjectMsg); !ok {
		t.Fatal("+ on the Projects tab didn't ask for a new project")
	}
	m.Press(len(projectsLabel)+1, actionsRow, tea.MouseLeft)
	m.Refresh(s, "", "")
	if m.Tab() != ChatTab {
		t.Fatalf("tab = %v after clicking Chat", m.Tab())
	}
	if _, ok := run(m.Press(m.width-2, actionsRow, tea.MouseLeft)).(NewChatMsg); !ok {
		t.Fatal("+ on the Chat tab didn't ask for a new chat")
	}
	m.Press(1, actionsRow, tea.MouseLeft)
	if m.Tab() != ProjectsTab {
		t.Fatalf("tab = %v after clicking Projects", m.Tab())
	}
}

func agents(t *testing.T) (*state.Store, Model) {
	t.Helper()
	s := store(t)
	for _, frame := range []string{
		`{"session_id":"a1","seq":2,"type":"task_created","task_id":"t1","description":"fix the streak bug","lane":"write","scope":[]}`,
		`{"session_id":"a1","seq":3,"type":"task_status_changed","task_id":"t1","status":"running","model_key":"claude-code"}`,
		`{"session_id":"a1","seq":4,"type":"task_created","task_id":"t2","description":"budget","lane":"write","scope":[]}`,
		`{"session_id":"a1","seq":5,"type":"task_status_changed","task_id":"t2","status":"done","model_key":"codex"}`,
		`{"session_id":"b1","seq":2,"type":"session_ended","status":"done","final_text":"ok"}`,
	} {
		ev, err := protocol.Decode([]byte(frame))
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		s.Apply(ev)
	}
	m := model(t, s)
	m.SetTab(ChatTab)
	m.Refresh(s, "", "")
	return s, m
}

func TestTheChatTabListsOnlyRunningAgentsUnderTheirProject(t *testing.T) {
	_, m := agents(t)
	var kinds []rowKind
	for _, r := range m.rows {
		kinds = append(kinds, r.kind)
	}
	want := []rowKind{headRow, agentRow, gapRow, agentTaskRow, gapRow}
	if fmt.Sprint(kinds) != fmt.Sprint(want) {
		t.Fatalf("rows = %v, want %v", kinds, want)
	}
	lines := strings.Split(stripped(m.View(false)), "\n")
	if !strings.Contains(lines[listTop], "shop") {
		t.Fatalf("no project name above the agents: %q", lines[listTop])
	}
	if !strings.Contains(lines[listTop+3], "claude-code") || !strings.Contains(lines[listTop+3], "fix the streak") {
		t.Fatalf("agent line = %q", lines[listTop+3])
	}
	view := strings.Join(lines, "\n")
	if strings.Contains(view, "codex") || strings.Contains(view, "add a route") {
		t.Fatalf("a finished agent or chat is listed:\n%s", view)
	}
	if !strings.Contains(lines[actionsRow], "Chat 2") {
		t.Fatalf("the tab doesn't count the agents: %q", lines[actionsRow])
	}
}

func TestAgentRectanglesAreOneRowWithOneBlankRowBetween(t *testing.T) {
	_, m := agents(t)
	lines := strings.Split(stripped(m.View(false)), "\n")
	first, gap, second := lines[listTop+1], lines[listTop+2], lines[listTop+3]
	if strings.TrimSpace(gap) != "" {
		t.Fatalf("the row between two agents isn't blank: %q", gap)
	}
	for _, line := range []string{first, second} {
		if !strings.Contains(line, "■") {
			t.Fatalf("an agent row doesn't start with its colour box: %q", line)
		}
	}
	for i, line := range lines {
		if w := ansi.StringWidth(line); w != m.width {
			t.Fatalf("line %d is %d wide, want %d: %q", i, w, m.width, line)
		}
	}
}

func TestArrowsSkipTheGapsAndClickingAnAgentOpensIt(t *testing.T) {
	_, m := agents(t)
	if m.rows[m.cursor].kind != agentRow {
		t.Fatalf("cursor starts on %v", m.rows[m.cursor].kind)
	}
	m.Key(key("j"))
	if m.rows[m.cursor].kind != agentTaskRow {
		t.Fatalf("down landed on %v", m.rows[m.cursor].kind)
	}
	if cmd := m.Press(3, listTop+2, tea.MouseLeft); cmd != nil {
		t.Fatal("a click on the gap did something")
	}
	m.Press(3, listTop+3, tea.MouseLeft)
	got, ok := run(m.Release(false)).(SelectMsg)
	if !ok || got.SessionID != "a1" || got.TaskID != "t1" {
		t.Fatalf("select = %#v", got)
	}
}

func TestSharedChatsCarryAMark(t *testing.T) {
	m := model(t, store(t))
	shared, private := stripped(m.renderRow(m.rows[1], false)), stripped(m.renderRow(m.rows[4], false))
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

func TestTheProjectsTabDrawsChatsAsRectanglesWithAWorkerBox(t *testing.T) {
	s, m := agents(t)
	m.SetTab(ProjectsTab)
	m.Refresh(s, "", "")
	for _, r := range m.rows {
		if r.kind == taskRow {
			t.Fatal("a chat that isn't open lists its agents")
		}
	}
	m.Refresh(s, "a1", "")
	var kinds []rowKind
	for _, r := range m.rows {
		kinds = append(kinds, r.kind)
	}
	want := []rowKind{projectRow, sessionRow, gapRow, taskRow, gapRow, taskRow, gapRow, projectRow, sessionRow, gapRow}
	if fmt.Sprint(kinds) != fmt.Sprint(want) {
		t.Fatalf("rows = %v, want %v", kinds, want)
	}
	lines := strings.Split(stripped(m.View(false)), "\n")
	if !strings.Contains(lines[listTop+1], "■ fix the cart") {
		t.Fatalf("chat line = %q", lines[listTop+1])
	}
	if !strings.Contains(lines[listTop+3], "■ claude-code") || !strings.Contains(lines[listTop+5], "■ codex") {
		t.Fatalf("agent lines = %q, %q", lines[listTop+3], lines[listTop+5])
	}
	claude, codex := m.renderRow(m.rows[3], false), m.renderRow(m.rows[5], false)
	if !strings.Contains(claude, "255;122;69") || !strings.Contains(codex, "62;207;142") {
		t.Fatalf("the worker boxes aren't orange and green:\n%q\n%q", claude, codex)
	}
}

func TestOnlyClaudeCodeAndCodexAreNamedOnTheirRectangles(t *testing.T) {
	s, m := agents(t)
	for _, frame := range []string{
		`{"session_id":"a1","seq":6,"type":"task_created","task_id":"t3","description":"sort the downloads folder\nand list duplicates","lane":"write","scope":[]}`,
		`{"session_id":"a1","seq":7,"type":"task_status_changed","task_id":"t3","status":"running","model_key":"deepseek-flash"}`,
	} {
		ev, err := protocol.Decode([]byte(frame))
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		s.Apply(ev)
	}
	m.SetTab(ProjectsTab)
	m.Refresh(s, "a1", "")
	view := stripped(m.View(false))
	for _, want := range []string{"■ claude-code fix the streak", "■ codex budget", "■ sort the downloads folder"} {
		if !strings.Contains(view, want) {
			t.Fatalf("no %q in:\n%s", want, view)
		}
	}
	if strings.Contains(view, "deepseek") || strings.Contains(view, "and list duplicates") {
		t.Fatalf("a Fox agent shows its model, or more than its first line:\n%s", view)
	}
}

func TestAPausedChatShowsNoStatusWord(t *testing.T) {
	s := store(t)
	ev, err := protocol.Decode([]byte(`{"session_id":"b1","seq":2,"type":"session_ended","status":"paused","final_text":"paused: idle"}`))
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	s.Apply(ev)
	m := model(t, s)
	line := strings.TrimSpace(stripped(m.renderRow(m.rows[4], false)))
	if line != "■ add a route" {
		t.Fatalf("paused chat row = %q", line)
	}
	for _, e := range s.Session("b1").Entries {
		if strings.Contains(e.Text, "send a message to continue") {
			t.Fatalf("a paused chat still gets the notice: %q", e.Text)
		}
	}
}
