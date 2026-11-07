package app

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"

	"fox/tui/internal/ui/board"
)

var seq = 10

func draw(t *testing.T, m *Model, args map[string]any, result string) {
	t.Helper()
	body, _ := json.Marshal(args)
	seq++
	event(t, m, fmt.Sprintf(`{"session_id":"s1","seq":%d,"type":"tool_call","name":"board","args":%s}`, seq, body))
	content, _ := json.Marshal(result)
	seq++
	event(t, m, fmt.Sprintf(`{"session_id":"s1","seq":%d,"type":"tool_result","name":"board","content":%s}`, seq, content))
}

func TestCtrlBOpensTheBoard(t *testing.T) {
	m, _ := activeChat(t, 160)
	m.update(tea.KeyPressMsg{Code: 'b', Mod: tea.ModCtrl})
	m.sync()
	if !m.boardOpen || m.focus != focusSide || !m.frame.HasSide {
		t.Fatalf("open %v focus %v", m.boardOpen, m.focus)
	}
	if !strings.Contains(ansi.Strip(m.render()), "Nothing on the board yet") {
		t.Fatal("an empty board has no hint")
	}
	ctrlD(m)
	if m.boardOpen || !m.diffOpen {
		t.Fatal("ctrl+d should swap the board for the diff")
	}
}

func TestADrawingOpensTheBoardWithoutTakingFocus(t *testing.T) {
	m, _ := activeChat(t, 160)
	draw(t, m, map[string]any{"title": "How a request flows", "diagram": "graph LR\n  TUI --> Hub --> Worker",
		"note": "The hub **routes** every message."}, "Added it.")
	if !m.boardOpen || m.focus != focusComposer {
		t.Fatalf("open %v focus %v", m.boardOpen, m.focus)
	}
	view := ansi.Strip(m.render())
	for _, want := range []string{"board · 1 card", "1 How a request flows", "│ TUI ├", "►│ Hub ├", "routes"} {
		if !strings.Contains(view, want) {
			t.Fatalf("missing %q:\n%s", want, view)
		}
	}
}

func TestRefusedAndClearedCards(t *testing.T) {
	m, _ := activeChat(t, 160)
	draw(t, m, map[string]any{"title": "one"}, "ok")
	draw(t, m, map[string]any{"title": "bad", "diagram": "pie"}, `{"error": "nope"}`)
	draw(t, m, map[string]any{"title": "two"}, "ok")
	cards := board.Cards(m.store.Session("s1"))
	if len(cards) != 2 || cards[1].Title != "two" {
		t.Fatalf("cards %+v", cards)
	}
	draw(t, m, map[string]any{"title": "fresh", "clear": true}, "ok")
	cards = board.Cards(m.store.Session("s1"))
	if len(cards) != 1 || cards[0].Title != "fresh" {
		t.Fatalf("cards %+v", cards)
	}
}

func TestABrokenDiagramShowsItsSource(t *testing.T) {
	m, _ := activeChat(t, 160)
	draw(t, m, map[string]any{"diagram": "graph LR\n  A -->"}, "ok")
	view := ansi.Strip(m.render())
	if !strings.Contains(view, "A -->") || !strings.Contains(view, "not drawn") {
		t.Fatalf("view:\n%s", view)
	}
}

func TestTheBoardScrollsAndJumpsBetweenCards(t *testing.T) {
	m, _ := activeChat(t, 160)
	for i := range 6 {
		draw(t, m, map[string]any{"title": fmt.Sprintf("step %d", i+1),
			"diagram": "graph TD\n  A --> B\n  B --> C\n  C --> D"}, "ok")
	}
	m.update(tea.KeyPressMsg{Code: 'b', Mod: tea.ModCtrl})
	m.update(tea.KeyPressMsg{Code: 'b', Mod: tea.ModCtrl})
	m.sync()
	press(m, 'k')
	m.update(tea.KeyPressMsg{Code: tea.KeyHome})
	m.sync()
	if !strings.Contains(ansi.Strip(m.render()), "1 step 1") {
		t.Fatalf("home did not reach the first card:\n%s", ansi.Strip(m.render()))
	}
	press(m, 'n')
	press(m, 'n')
	if !strings.Contains(ansi.Strip(m.render()), "3 step 3") {
		t.Fatalf("n did not move to card 3:\n%s", ansi.Strip(m.render()))
	}
}

func TestADrawingWhileTheDiffIsOpenOnlyFlashes(t *testing.T) {
	m, _ := activeChat(t, 160)
	ctrlD(m)
	draw(t, m, map[string]any{"title": "x"}, "ok")
	if !m.diffOpen || m.boardOpen || !strings.Contains(m.flash, "ctrl+b") {
		t.Fatalf("diff %v board %v flash %q", m.diffOpen, m.boardOpen, m.flash)
	}
}
