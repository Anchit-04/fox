package board

import (
	"fmt"
	"strings"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"

	"fox/tui/internal/state"
	"fox/tui/internal/ui/theme"
	"fox/tui/internal/ui/transcript"
)

const (
	Tool    = "board"
	chrome  = 2
	panStep = 8
)

type Card struct {
	Title   string
	Diagram string
	Note    string
	TaskID  string
}

type CloseMsg struct{}

type BlurMsg struct{}

type call struct {
	card  Card
	clear bool
}

func Cards(sess *state.Session) []Card {
	if sess == nil {
		return nil
	}
	var out []Card
	pending := map[string][]call{}
	for _, e := range sess.Entries {
		if e.Tool != Tool {
			continue
		}
		switch e.Kind {
		case state.ToolCallEntry:
			pending[e.TaskID] = append(pending[e.TaskID], call{card: Card{Title: text(e.Args, "title"),
				Diagram: text(e.Args, "diagram"), Note: text(e.Args, "note"), TaskID: e.TaskID},
				clear: e.Args["clear"] == true})
		case state.ToolResultEntry:
			queue := pending[e.TaskID]
			if len(queue) == 0 {
				continue
			}
			c := queue[0]
			pending[e.TaskID] = queue[1:]
			if strings.HasPrefix(strings.TrimSpace(e.Text), `{"error"`) {
				continue
			}
			if c.clear {
				out = nil
			}
			if c.card.Title != "" || c.card.Diagram != "" || c.card.Note != "" {
				out = append(out, c.card)
			}
		}
	}
	return out
}

func text(args map[string]any, key string) string {
	s, _ := args[key].(string)
	return strings.TrimSpace(s)
}

type Model struct {
	theme  *theme.Theme
	scroll int
	pan    int
	width  int
	height int
	starts []int
	total  int
	follow bool
}

func New(th *theme.Theme) Model {
	return Model{theme: th, follow: true}
}

func (m *Model) SetSize(width, height int) {
	m.width, m.height = width, height
}

func Handles(key string) bool {
	switch key {
	case "j", "k", "up", "down", "h", "l", "left", "right", "n", "p", "]", "[", "q", "pgup", "pgdown", "space",
		"home", "end":
		return true
	}
	return false
}

func (m *Model) Key(msg tea.KeyPressMsg) tea.Cmd {
	switch msg.String() {
	case "j", "down":
		m.Scroll(1)
	case "k", "up":
		m.Scroll(-1)
	case "pgdown", "space":
		m.Scroll(max(m.body()-1, 1))
	case "pgup":
		m.Scroll(-max(m.body()-1, 1))
	case "home":
		m.scroll, m.follow = 0, false
	case "end":
		m.scroll = m.maxScroll()
	case "l", "right":
		m.pan += panStep
	case "h", "left":
		m.pan = max(m.pan-panStep, 0)
	case "n", "]":
		m.jump(1)
	case "p", "[":
		m.jump(-1)
	case "q":
		return func() tea.Msg { return CloseMsg{} }
	case "esc":
		return func() tea.Msg { return BlurMsg{} }
	}
	return nil
}

func (m *Model) Scroll(delta int) {
	m.scroll = min(max(m.scroll+delta, 0), m.maxScroll())
	m.follow = false
}

func (m *Model) jump(delta int) {
	current := 0
	for i, s := range m.starts {
		if s <= m.scroll {
			current = i
		}
	}
	next := min(max(current+delta, 0), len(m.starts)-1)
	if next >= 0 && next < len(m.starts) {
		m.scroll = min(m.starts[next], m.maxScroll())
	}
	m.follow = false
}

func (m *Model) Follow() {
	m.follow = true
}

func (m Model) body() int { return max(m.height-chrome, 1) }

func (m Model) maxScroll() int { return max(m.total-m.body(), 0) }

func (m *Model) Layout(cards []Card) []string {
	th := m.theme
	inner := max(m.width-2, 10)
	var lines []string
	m.starts = m.starts[:0]
	for i, c := range cards {
		if i > 0 {
			lines = append(lines, "")
		}
		m.starts = append(m.starts, len(lines))
		head := th.Accent.Render(fmt.Sprintf("%d", i+1))
		if c.Title != "" {
			head += " " + th.Strong.Render(c.Title)
		}
		if c.TaskID != "" {
			head += th.Faint.Render(" · exec " + c.TaskID)
		}
		lines = append(lines, head)
		if c.Diagram != "" {
			lines = append(lines, "")
			lines = append(lines, transcript.Diagram(th, c.Diagram, inner, false)...)
		}
		if c.Note != "" {
			lines = append(lines, "")
			lines = append(lines, strings.Split(transcript.Markdown(th, c.Note, inner), "\n")...)
		}
	}
	m.total = len(lines)
	if m.follow && len(m.starts) > 0 {
		m.scroll = min(m.starts[len(m.starts)-1], m.maxScroll())
	}
	m.scroll = min(m.scroll, m.maxScroll())
	return lines
}

func (m *Model) View(cards []Card, focused bool) string {
	th := m.theme
	w := max(m.width, 1)
	lines := m.Layout(cards)
	title := th.Strong.Render("board")
	if len(cards) > 0 {
		title += th.Muted.Render(fmt.Sprintf(" · %d card%s", len(cards), plural(len(cards))))
	}
	if focused {
		title = th.Accent.Render("▌") + title
	} else {
		title = " " + title
	}
	hint := "h/l pan "
	if m.pan == 0 {
		hint = ""
	}
	right := th.Faint.Render(hint)
	gap := max(w-lipgloss.Width(title)-lipgloss.Width(right), 1)
	rows := []string{ansi.Truncate(title+strings.Repeat(" ", gap)+right, w, ""), th.Rule.Render(strings.Repeat("─", w))}
	if len(cards) == 0 {
		rows = append(rows, th.Muted.Width(w).Render(" Nothing on the board yet. Ask fox to draw how something works, "+
			"or to explain it step by step on the board."))
	}
	end := min(m.scroll+m.body(), len(lines))
	for _, line := range lines[min(m.scroll, end):end] {
		rows = append(rows, " "+ansi.Truncate(ansi.TruncateLeft(line, m.pan, ""), max(w-1, 1), "…"))
	}
	for len(rows) < m.height {
		rows = append(rows, "")
	}
	if len(rows) > m.height {
		rows = rows[:m.height]
	}
	return lipgloss.NewStyle().Width(w).MaxWidth(w).Render(strings.Join(rows, "\n"))
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}
