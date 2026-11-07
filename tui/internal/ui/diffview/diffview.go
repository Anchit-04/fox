package diffview

import (
	"fmt"
	"strings"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"

	"fox/tui/internal/protocol"
	"fox/tui/internal/ui/theme"
)

const (
	Fox = "fox"
	Git = "git"

	minListRows = 3
	chrome      = 3
)

type CloseMsg struct{}

type BlurMsg struct{}

type SourceMsg struct {
	Source string
}

type Model struct {
	theme    *theme.Theme
	session  string
	source   string
	files    []protocol.DiffFile
	note     string
	selected int
	scroll   int
	loading  bool
	width    int
	height   int
}

func New(th *theme.Theme) Model {
	return Model{theme: th, source: Fox}
}

func (m *Model) Open(session string) {
	if session != m.session {
		m.session, m.files, m.note, m.selected, m.scroll = session, nil, "", 0, 0
	}
	m.loading = true
}

func (m Model) Session() string { return m.session }

func (m Model) Source() string { return m.source }

func (m Model) Files() []protocol.DiffFile { return m.files }

func (m Model) Selected() int { return m.selected }

func (m *Model) SetSource(source string) {
	if source != m.source {
		m.source, m.selected, m.scroll = source, 0, 0
	}
	m.loading = true
}

func (m *Model) Set(p protocol.FileDiffPayload) {
	current := ""
	if m.selected < len(m.files) {
		current = m.files[m.selected].Path
	}
	m.files, m.note, m.loading = p.Files, p.Note, false
	m.selected = 0
	for i, f := range m.files {
		if f.Path == current {
			m.selected = i
		}
	}
	m.clamp()
}

func (m *Model) SetSize(width, height int) {
	m.width, m.height = width, height
	m.clamp()
}

func Handles(key string) bool {
	switch key {
	case "j", "k", "up", "down", "n", "p", "]", "[", "g", "q", "pgup", "pgdown", "space", "home", "end":
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
		m.Scroll(max(m.bodyRows()-1, 1))
	case "pgup":
		m.Scroll(-max(m.bodyRows()-1, 1))
	case "home":
		m.scroll = 0
	case "end":
		m.scroll = m.maxScroll()
	case "n", "]":
		m.Select(m.selected + 1)
	case "p", "[":
		m.Select(m.selected - 1)
	case "g":
		next := Git
		if m.source == Git {
			next = Fox
		}
		return func() tea.Msg { return SourceMsg{Source: next} }
	case "q":
		return func() tea.Msg { return CloseMsg{} }
	case "esc":
		return func() tea.Msg { return BlurMsg{} }
	}
	return nil
}

func (m *Model) Select(i int) {
	if len(m.files) == 0 {
		return
	}
	i = min(max(i, 0), len(m.files)-1)
	if i != m.selected {
		m.selected, m.scroll = i, 0
	}
}

func (m *Model) Scroll(delta int) {
	m.scroll += delta
	m.clamp()
}

func (m *Model) Press(x, y int) {
	top := chrome - 1
	rows := m.listRows()
	if y < top || y >= top+rows {
		return
	}
	m.Select(m.listStart() + y - top)
}

func (m Model) listRows() int {
	if len(m.files) == 0 {
		return 0
	}
	return min(len(m.files), max(minListRows, m.height/4))
}

func (m Model) listStart() int {
	rows := m.listRows()
	if rows == 0 || m.selected < rows {
		return 0
	}
	return min(m.selected-rows+1, len(m.files)-rows)
}

func (m Model) bodyRows() int {
	return max(m.height-chrome-m.listRows(), 1)
}

func (m Model) lines() []string {
	if m.selected >= len(m.files) {
		return nil
	}
	f := m.files[m.selected]
	var out []string
	if f.Diff != "" {
		out = strings.Split(f.Diff, "\n")
	}
	if f.Note != "" {
		out = append(out, "", "("+f.Note+")")
	}
	return out
}

func (m Model) maxScroll() int {
	return max(len(m.lines())-m.bodyRows(), 0)
}

func (m *Model) clamp() {
	if m.selected >= len(m.files) {
		m.selected = max(len(m.files)-1, 0)
	}
	m.scroll = min(max(m.scroll, 0), m.maxScroll())
}

func (m Model) View(focused bool) string {
	th := m.theme
	w := max(m.width, 1)
	title := th.Strong.Render("changes")
	other := "git diff"
	if m.source == Git {
		title += th.Muted.Render(" · git diff")
		other = "fox's changes"
	} else {
		title += th.Muted.Render(" · made by fox in this chat")
	}
	if focused {
		title = th.Accent.Render("▌") + title
	} else {
		title = " " + title
	}
	right := th.Faint.Render("g " + other + " ")
	gap := max(w-lipgloss.Width(title)-lipgloss.Width(right), 1)
	rows := []string{ansi.Truncate(title+strings.Repeat(" ", gap)+right, w, ""), th.Rule.Render(strings.Repeat("─", w))}

	switch {
	case m.loading && len(m.files) == 0:
		rows = append(rows, th.Muted.Render(" loading…"))
	case len(m.files) == 0:
		note := m.note
		if note == "" {
			note = "Nothing to show."
		}
		rows = append(rows, th.Muted.Width(w).Render(" "+note))
	default:
		start := m.listStart()
		for i := start; i < start+m.listRows(); i++ {
			rows = append(rows, m.fileRow(m.files[i], i == m.selected, w))
		}
		rows = append(rows, th.Rule.Render(strings.Repeat("─", w)))
		lines := m.lines()
		end := min(m.scroll+m.bodyRows(), len(lines))
		for _, line := range lines[m.scroll:end] {
			rows = append(rows, m.diffLine(line, w))
		}
	}
	for len(rows) < m.height {
		rows = append(rows, "")
	}
	if len(rows) > m.height {
		rows = rows[:m.height]
	}
	return lipgloss.NewStyle().Width(w).MaxWidth(w).Render(strings.Join(rows, "\n"))
}

func (m Model) fileRow(f protocol.DiffFile, selected bool, w int) string {
	th := m.theme
	counts := th.Added.Render(fmt.Sprintf("+%d", f.Added)) + " " + th.Removed.Render(fmt.Sprintf("-%d", f.Removed))
	marker, style := "  ", th.Text
	if selected {
		marker, style = th.Accent.Render("▸ "), th.Strong
	}
	left := marker + th.Faint.Render(statusLetter(f.Status)+" ")
	room := max(w-lipgloss.Width(left)-lipgloss.Width(counts)-2, 4)
	path := style.Render(ansi.TruncateLeft(f.Path, max(len(f.Path)-room, 0), "…"))
	gap := max(w-lipgloss.Width(left)-lipgloss.Width(path)-lipgloss.Width(counts)-1, 1)
	return left + path + strings.Repeat(" ", gap) + counts
}

func statusLetter(status string) string {
	switch status {
	case "added":
		return "A"
	case "deleted":
		return "D"
	case "untracked":
		return "?"
	case "renamed":
		return "R"
	}
	return "M"
}

func (m Model) diffLine(line string, w int) string {
	th := m.theme
	line = ansi.Truncate(strings.ReplaceAll(line, "\t", "    "), max(w-1, 1), "…")
	switch {
	case strings.HasPrefix(line, "+++"), strings.HasPrefix(line, "---"):
		return " " + th.Faint.Render(line)
	case strings.HasPrefix(line, "@@"):
		return " " + th.Accent.Render(line)
	case strings.HasPrefix(line, "+"):
		return " " + th.Added.Render(line)
	case strings.HasPrefix(line, "-"):
		return " " + th.Removed.Render(line)
	case strings.HasPrefix(line, "("):
		return " " + th.Muted.Render(line)
	}
	return " " + th.Text.Render(line)
}
