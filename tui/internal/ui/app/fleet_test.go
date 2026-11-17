package app

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"

	"fox/tui/internal/client"
	"fox/tui/internal/protocol"
	"fox/tui/internal/ui/sidebar"
)

func delegating(t *testing.T, width int) *Model {
	t.Helper()
	m, _ := activeChat(t, width)
	event(t, m, `{"type":"task_created","session_id":"s1","seq":2,"task_id":"t1","description":"audit the search path","lane":"read","scope":[]}`)
	event(t, m, `{"type":"task_created","session_id":"s1","seq":3,"task_id":"t2","description":"fix neighbour pruning","lane":"write","scope":["index.go"]}`)
	event(t, m, `{"type":"task_status_changed","session_id":"s1","seq":4,"task_id":"t1","status":"running","model_key":"codex"}`)
	event(t, m, `{"type":"task_status_changed","session_id":"s1","seq":5,"task_id":"t2","status":"running","model_key":"claude-code"}`)
	event(t, m, `{"type":"tool_call","session_id":"s1","seq":6,"task_id":"t2","name":"edit_file","args":{"path":"index.go"}}`)
	return m
}

func plain(m *Model) string {
	return ansi.Strip(m.render())
}

func TestTheGridOpensWhenAChatDelegates(t *testing.T) {
	m := delegating(t, 160)
	if !m.grid || !m.frame.HasSidebar || m.sidebar.Tab() != sidebar.ChatTab {
		t.Fatalf("grid = %v, sidebar = %v", m.grid, m.frame.HasSidebar)
	}
	out := plain(m)
	for _, want := range []string{"T1 CODEX", "T2 CLAUDE-CODE", "RUN", "edit_file index.go", "SPLIT", "TOTAL"} {
		if !strings.Contains(out, want) {
			t.Fatalf("screen lacks %q:\n%s", want, out)
		}
	}
	if got := strings.Count(out, "\n") + 1; got != 40 {
		t.Fatalf("screen is %d rows, want 40", got)
	}
}

func TestClickingAnAgentOpensItAndEscReturns(t *testing.T) {
	m := delegating(t, 160)
	id, ok := m.fleet.TaskAt(m.gridRect.X+m.gridRect.W-2, m.gridRect.Y+1)
	if !ok || id != "t2" {
		t.Fatalf("pane under the click = %q, %v", id, ok)
	}
	m.update(tea.MouseClickMsg{X: m.gridRect.X + m.gridRect.W - 2, Y: m.gridRect.Y + 1, Button: tea.MouseLeft})
	m.sync()
	if m.activeTask != "t2" || m.gridShown() {
		t.Fatalf("task = %q, grid shown = %v", m.activeTask, m.gridShown())
	}
	if out := plain(m); !strings.Contains(out, "esc back to the grid") {
		t.Fatalf("the open agent doesn't say how to go back:\n%s", out)
	}
	m.update(tea.KeyPressMsg{Code: tea.KeyEscape})
	m.sync()
	if m.activeTask != "" || !m.gridShown() {
		t.Fatalf("task = %q, grid shown = %v", m.activeTask, m.gridShown())
	}
}

func TestTabPicksAnAgentAndEnterOpensIt(t *testing.T) {
	m := delegating(t, 160)
	m.update(tea.KeyPressMsg{Code: tea.KeyTab})
	m.update(tea.KeyPressMsg{Code: tea.KeyTab})
	m.sync()
	if m.fleet.Focused() != "t2" {
		t.Fatalf("focused = %q", m.fleet.Focused())
	}
	m.update(tea.KeyPressMsg{Code: tea.KeyEnter})
	m.sync()
	if m.activeTask != "t2" {
		t.Fatalf("task = %q", m.activeTask)
	}
}

func TestTheSplitButtonClosesTheGridAndTheSidebarReturns(t *testing.T) {
	m := delegating(t, 120)
	r, ok := m.splitButton()
	if !ok {
		t.Fatal("no split button")
	}
	m.update(tea.MouseClickMsg{X: r.X + 1, Y: r.Y, Button: tea.MouseLeft})
	m.sync()
	if m.grid || !m.frame.HasSidebar {
		t.Fatalf("grid = %v, sidebar = %v", m.grid, m.frame.HasSidebar)
	}
	m.update(tea.KeyPressMsg{Code: 'f', Mod: tea.ModCtrl})
	m.sync()
	if !m.grid || m.frame.HasSidebar {
		t.Fatalf("grid = %v, sidebar = %v", m.grid, m.frame.HasSidebar)
	}
}

func TestANarrowWindowShowsTheGridWithoutTheChatColumn(t *testing.T) {
	m := delegating(t, 80)
	if m.chat.W != 0 || m.gridRect.W == 0 {
		t.Fatalf("chat = %+v, grid = %+v", m.chat, m.gridRect)
	}
	for _, line := range strings.Split(plain(m), "\n") {
		if w := ansi.StringWidth(line); w > 80 {
			t.Fatalf("a line is %d wide: %q", w, line)
		}
	}
}

func TestMoreAgentsThanFitFoldTheFinishedOnes(t *testing.T) {
	m := delegating(t, 160)
	m.update(tea.WindowSizeMsg{Width: 160, Height: 24})
	m.update(settledMsg{seq: m.sizeSeq})
	for i := 3; i <= 9; i++ {
		event(t, m, fmt.Sprintf(`{"type":"task_created","session_id":"s1","seq":%d,"task_id":"t%d","description":"job %d","lane":"read","scope":[]}`, 10+i*2, i, i))
		event(t, m, fmt.Sprintf(`{"type":"task_status_changed","session_id":"s1","seq":%d,"task_id":"t%d","status":"done","model_key":"codex"}`, 11+i*2, i))
	}
	out := plain(m)
	if !strings.Contains(out, "T3 codex DONE") {
		t.Fatalf("the finished agents aren't in the strip:\n%s", out)
	}
	if !strings.Contains(out, "T1 CODEX") || !strings.Contains(out, "T2 CLAUDE-CODE") {
		t.Fatalf("the running agents lost their panes:\n%s", out)
	}
}

func TestAResizeKeepsTheOldWrapUntilItSettles(t *testing.T) {
	m, _ := activeChat(t, 120)
	before := m.transcript.Width()
	cmd := m.update(tea.WindowSizeMsg{Width: 100, Height: 40})
	m.sync()
	if !m.settling || m.transcript.Width() != before {
		t.Fatalf("settling = %v, width %d → %d", m.settling, before, m.transcript.Width())
	}
	m.update(cmd())
	m.sync()
	if m.settling || m.transcript.Width() == before {
		t.Fatalf("settling = %v, width still %d", m.settling, m.transcript.Width())
	}
}

func TestRepeatedToolCallsShareOneLine(t *testing.T) {
	m := delegating(t, 160)
	for i := 0; i < 5; i++ {
		event(t, m, fmt.Sprintf(`{"type":"tool_call","session_id":"s1","seq":%d,"task_id":"t1","name":"web_search","args":{"query":"laptop %d"}}`, 20+i*2, i))
		event(t, m, fmt.Sprintf(`{"type":"tool_result","session_id":"s1","seq":%d,"task_id":"t1","name":"web_search","content":"results"}`, 21+i*2))
	}
	event(t, m, `{"type":"tool_call","session_id":"s1","seq":40,"task_id":"t1","name":"web_fetch","args":{"url":"https://example.com"}}`)
	out := plain(m)
	if !strings.Contains(out, "web_search ×5 laptop 4") {
		t.Fatalf("the five searches aren't one line with the latest query:\n%s", out)
	}
	if strings.Contains(out, "laptop 3") {
		t.Fatalf("an older search still has its own line:\n%s", out)
	}
	if !strings.Contains(out, "web_fetch https://example.com") {
		t.Fatalf("the fetch after them is missing:\n%s", out)
	}
}

func TestReplayARecordedRun(t *testing.T) {
	path := os.Getenv("FOX_REPLAY")
	if path == "" {
		t.Skip("set FOX_REPLAY to a JSON list of events")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var frames []json.RawMessage
	if err := json.Unmarshal(data, &frames); err != nil {
		t.Fatal(err)
	}
	m, _ := newApp(t)
	m.update(tea.WindowSizeMsg{Width: 170, Height: 46})
	m.update(settledMsg{seq: m.sizeSeq})
	for i, frame := range frames {
		ev, err := protocol.Decode(frame)
		if err != nil {
			t.Fatal(err)
		}
		m.update(client.EventMsg{Event: ev})
		if i == 0 {
			m.selectSession(ev.SessionID, "")
		}
		m.sync()
		if i == len(frames)/3 || i == len(frames)-1 {
			fmt.Printf("\n===== after event %d of %d =====\n%s\n", i+1, len(frames), plain(m))
		}
	}
}
