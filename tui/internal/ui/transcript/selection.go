package transcript

import (
	"math"
	"strings"

	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"
)

type point struct {
	line, col int
}

type selection struct {
	from, to point
	dragging bool
	shown    bool
}

func (s selection) ordered() (point, point) {
	a, b := s.from, s.to
	if b.line < a.line || b.line == a.line && b.col < a.col {
		a, b = b, a
	}
	return a, b
}

func (s selection) span(line int) (int, int, bool) {
	if !s.shown {
		return 0, 0, false
	}
	a, b := s.ordered()
	if line < a.line || line > b.line {
		return 0, 0, false
	}
	from, to := 0, math.MaxInt
	if line == a.line {
		from = a.col
	}
	if line == b.line {
		to = b.col + 1
	}
	return from, to, true
}

func (m *Model) Press(x, y int) {
	p := m.at(x, y)
	if m.toggleAt(p.line) {
		m.ClearSelection()
		return
	}
	m.sel = selection{from: p, to: p, dragging: true}
	m.paint()
}

func (m *Model) Drag(x, y int) {
	if !m.sel.dragging {
		return
	}
	switch {
	case y < 0:
		m.view.ScrollUp(1)
	case y >= m.view.Height():
		m.view.ScrollDown(1)
	}
	m.sel.to = m.at(x, y)
	m.sel.shown = m.sel.to != m.sel.from
	m.paint()
}

func (m *Model) Release() {
	m.sel.dragging = false
}

func (m Model) Dragging() bool {
	return m.sel.dragging
}

func (m Model) HasSelection() bool {
	return m.sel.shown
}

func (m *Model) ClearSelection() {
	if m.sel == (selection{}) {
		return
	}
	m.sel = selection{}
	m.paint()
}

func (m Model) Selection() string {
	if !m.sel.shown {
		return ""
	}
	a, b := m.sel.ordered()
	var out []string
	for i := a.line; i <= b.line && i < len(m.lines); i++ {
		from, to, _ := m.sel.span(i)
		out = append(out, plain(ansi.Cut(m.lines[i], from, min(to, ansi.StringWidth(m.lines[i])))))
	}
	return strings.TrimSpace(strings.Join(out, "\n"))
}

func (m Model) at(x, y int) point {
	line := min(max(m.view.YOffset()+min(max(y, 0), m.view.Height()-1), 0), max(len(m.lines)-1, 0))
	return point{line: line, col: max(x, 0)}
}

func (m *Model) paint() {
	if !m.sel.shown {
		m.view.SetContent(strings.Join(m.lines, "\n"))
		return
	}
	hl := lipgloss.NewStyle().Background(m.theme.Selection).Foreground(m.theme.Ink)
	out := make([]string, len(m.lines))
	for i, line := range m.lines {
		from, to, ok := m.sel.span(i)
		if !ok {
			out[i] = line
			continue
		}
		w := ansi.StringWidth(line)
		to = min(to, w)
		if from >= to {
			out[i] = line
			continue
		}
		out[i] = ansi.Truncate(line, from, "") + "\x1b[m" +
			hl.Render(ansi.Strip(ansi.Cut(line, from, to))) +
			ansi.TruncateLeft(line, to, "")
	}
	m.view.SetContent(strings.Join(out, "\n"))
}

func plain(s string) string {
	s = strings.TrimRight(ansi.Strip(s), " ")
	for _, gutter := range []string{"┃ ", "│ "} {
		if rest, ok := strings.CutPrefix(s, gutter); ok {
			return rest
		}
	}
	return s
}
