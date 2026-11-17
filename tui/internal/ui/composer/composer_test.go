package composer

import (
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"

	"fox/tui/internal/ui/theme"
)

func typed(m *Model, text string) {
	for _, r := range text {
		m.Update(tea.KeyPressMsg{Code: r, Text: string(r)})
	}
}

func TestTheBoxGrowsAndKeepsTheFirstLine(t *testing.T) {
	m := New(theme.New("", ""))
	m.SetWidth(40)
	before := m.Height()
	typed(&m, "first words of the prompt and then quite a lot more text that has to wrap onto further rows")
	if m.Height() <= before {
		t.Fatalf("height stayed at %d", m.Height())
	}
	if out := ansi.Strip(m.View("")); !strings.Contains(out, "first words") {
		t.Fatalf("the first line scrolled away:\n%s", out)
	}
}

func TestTheBoxShrinksBackAfterSending(t *testing.T) {
	m := New(theme.New("", ""))
	m.SetWidth(40)
	empty := m.Height()
	typed(&m, strings.Repeat("word ", 30))
	m.Update(tea.KeyPressMsg{Code: tea.KeyEnter})
	if m.Height() != empty {
		t.Fatalf("height = %d after sending, want %d", m.Height(), empty)
	}
}

func TestALongPromptStopsGrowingAndScrolls(t *testing.T) {
	m := New(theme.New("", ""))
	m.SetWidth(40)
	typed(&m, strings.Repeat("word ", 200))
	if got := m.Height() - chrome; got != maxLines {
		t.Fatalf("input rows = %d, want %d", got, maxLines)
	}
	if !strings.Contains(m.Value(), strings.Repeat("word ", 199)) {
		t.Fatal("typing was cut off before the end of the prompt")
	}
}
