package app

import (
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"

	"fox/tui/internal/protocol"
)

const oneFile = `{"type":"file_diff","session_id":"s1","source":"fox","note":"","files":[` +
	`{"path":"shop/cart.py","status":"modified","added":1,"removed":1,"note":"",` +
	`"diff":"--- a/shop/cart.py\n+++ b/shop/cart.py\n@@ -1 +1 @@\n-old total\n+new total"}]}`

func (r *recorder) diffs() []protocol.FileDiff {
	var out []protocol.FileDiff
	for _, c := range r.sent {
		if d, ok := c.(protocol.FileDiff); ok {
			out = append(out, d)
		}
	}
	return out
}

func activeChat(t *testing.T, width int) (*Model, *recorder) {
	t.Helper()
	m, r := newApp(t)
	m.update(tea.WindowSizeMsg{Width: width, Height: 40})
	event(t, m, `{"session_id":"s1","seq":1,"type":"session_started","task":"t","workspace":"/w/shop"}`)
	m.selectSession("s1", "")
	m.sync()
	r.sent = nil
	return m, r
}

func ctrlD(m *Model) {
	m.update(tea.KeyPressMsg{Code: 'd', Mod: tea.ModCtrl})
	m.sync()
}

func press(m *Model, key rune) {
	cmd := m.update(tea.KeyPressMsg{Code: key, Text: string(key)})
	for cmd != nil {
		msg := cmd()
		if msg == nil {
			break
		}
		cmd = m.update(msg)
	}
	m.sync()
}

func TestCtrlDNeedsAChat(t *testing.T) {
	m, r := newApp(t)
	ctrlD(m)
	if m.diffOpen || len(r.diffs()) != 0 || !strings.Contains(m.flash, "start a chat first") {
		t.Fatalf("open %v sent %v flash %q", m.diffOpen, r.diffs(), m.flash)
	}
}

func TestCtrlDOpensThePaneBesideTheChat(t *testing.T) {
	m, r := activeChat(t, 160)
	ctrlD(m)
	if !m.diffOpen || m.focus != focusDiff {
		t.Fatalf("open %v focus %v", m.diffOpen, m.focus)
	}
	if got := r.diffs(); len(got) != 1 || got[0] != (protocol.FileDiff{SessionID: "s1", Source: "fox"}) {
		t.Fatalf("sent %+v", got)
	}
	f := m.frame
	if !f.HasDiff || f.DiffFull || f.Diff.X <= f.Main.X || f.Main.W+1+f.Diff.W+f.Sidebar.W+1 != 160 {
		t.Fatalf("frame %+v", f)
	}
	event(t, m, oneFile)
	view := ansi.Strip(m.render())
	for _, want := range []string{"changes", "shop/cart.py", "+1 -1", "-old total", "+new total", "ask fox"} {
		if !strings.Contains(view, want) {
			t.Fatalf("view is missing %q:\n%s", want, view)
		}
	}
}

func TestANarrowWindowGivesThePaneTheWholeColumn(t *testing.T) {
	m, _ := activeChat(t, 90)
	ctrlD(m)
	if !m.frame.DiffFull {
		t.Fatalf("frame %+v", m.frame)
	}
	m.update(tea.KeyPressMsg{Code: tea.KeyEscape})
	m.sync()
	if m.diffOpen {
		t.Fatal("esc in the full-width pane should close it")
	}
}

func TestEscGoesBackToTheChatAndCtrlDCloses(t *testing.T) {
	m, _ := activeChat(t, 160)
	ctrlD(m)
	m.update(tea.KeyPressMsg{Code: tea.KeyEscape})
	m.sync()
	if !m.diffOpen || m.focus != focusComposer {
		t.Fatalf("open %v focus %v", m.diffOpen, m.focus)
	}
	ctrlD(m)
	if m.diffOpen || m.frame.HasDiff {
		t.Fatal("ctrl+d did not close the pane")
	}
}

func TestGSwitchesToGitDiff(t *testing.T) {
	m, r := activeChat(t, 160)
	ctrlD(m)
	r.sent = nil
	press(m, 'g')
	if got := r.diffs(); len(got) != 1 || got[0].Source != "git" || m.diff.Source() != "git" {
		t.Fatalf("sent %+v source %q", got, m.diff.Source())
	}
	event(t, m, oneFile)
	if len(m.diff.Files()) != 0 {
		t.Fatal("a fox answer filled the git view")
	}
}

func TestEditsRefreshTheCountAndTheHeaderShowsIt(t *testing.T) {
	m, r := activeChat(t, 160)
	event(t, m, `{"session_id":"s1","seq":2,"type":"tool_result","name":"edit_file","content":"ok"}`)
	if got := r.diffs(); len(got) != 1 || got[0].Source != "fox" {
		t.Fatalf("sent %+v", got)
	}
	event(t, m, oneFile)
	if !strings.Contains(ansi.Strip(m.render()), "1 file changed · ctrl+d") {
		t.Fatalf("header:\n%s", ansi.Strip(m.render()))
	}
	r.sent = nil
	event(t, m, `{"session_id":"s1","seq":3,"type":"tool_result","name":"read_file","content":"x"}`)
	if len(r.diffs()) != 0 {
		t.Fatal("a read asked for a diff")
	}
}

func TestNAndPMoveBetweenFiles(t *testing.T) {
	m, _ := activeChat(t, 160)
	ctrlD(m)
	event(t, m, `{"type":"file_diff","session_id":"s1","source":"fox","files":[`+
		`{"path":"a.py","status":"modified","added":1,"removed":0,"diff":"+a"},`+
		`{"path":"b.py","status":"added","added":1,"removed":0,"diff":"+b"}]}`)
	press(m, 'n')
	if m.diff.Selected() != 1 {
		t.Fatalf("selected %d", m.diff.Selected())
	}
	press(m, 'n')
	press(m, 'p')
	if m.diff.Selected() != 0 {
		t.Fatalf("selected %d", m.diff.Selected())
	}
}

func TestTabReachesThePane(t *testing.T) {
	m, _ := activeChat(t, 160)
	ctrlD(m)
	m.update(tea.KeyPressMsg{Code: tea.KeyEscape})
	seen := map[focus]bool{}
	for range 3 {
		m.update(tea.KeyPressMsg{Code: tea.KeyTab})
		m.sync()
		seen[m.focus] = true
	}
	if !seen[focusDiff] || !seen[focusSidebar] || !seen[focusComposer] {
		t.Fatalf("tab visited %v", seen)
	}
}
