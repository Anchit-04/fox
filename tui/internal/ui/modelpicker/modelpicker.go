package modelpicker

import (
	"strings"

	"charm.land/bubbles/v2/textinput"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"

	"fox/tui/internal/protocol"
	"fox/tui/internal/ui/theme"
)

type PickMsg struct {
	ModelKey string
}

type CloseMsg struct{}

type OpenVaultMsg struct{}

const boxWidth = 52

type Model struct {
	theme   *theme.Theme
	filter  textinput.Model
	cursor  int
	current string
	note    string
}

func New(th *theme.Theme) Model {
	ti := textinput.New()
	ti.Prompt = "⌕ "
	ti.Placeholder = "filter"
	ti.SetWidth(boxWidth - 8)
	return Model{theme: th, filter: ti}
}

func (m *Model) Open(current, note string) tea.Cmd {
	m.current, m.note = current, note
	m.cursor = 0
	m.filter.Reset()
	return m.filter.Focus()
}

func (m *Model) visible(models []protocol.VaultEntry) []protocol.VaultEntry {
	q := strings.ToLower(strings.TrimSpace(m.filter.Value()))
	var ready, locked []protocol.VaultEntry
	for _, e := range models {
		if q != "" && !strings.Contains(strings.ToLower(e.ModelKey+" "+e.Price), q) {
			continue
		}
		if e.Present {
			ready = append(ready, e)
		} else {
			locked = append(locked, e)
		}
	}
	return append(ready, locked...)
}

func (m *Model) Update(msg tea.Msg, models []protocol.VaultEntry) tea.Cmd {
	rows := m.visible(models)
	if k, ok := msg.(tea.KeyPressMsg); ok {
		switch k.String() {
		case "esc":
			return emit(CloseMsg{})
		case "up", "ctrl+k":
			m.cursor = max(m.cursor-1, 0)
			return nil
		case "down", "ctrl+j":
			m.cursor = min(m.cursor+1, max(len(rows)-1, 0))
			return nil
		case "enter":
			if m.cursor >= len(rows) {
				return nil
			}
			return choose(rows[m.cursor])
		}
	}
	var cmd tea.Cmd
	m.filter, cmd = m.filter.Update(msg)
	m.cursor = min(m.cursor, max(len(m.visible(models))-1, 0))
	return cmd
}

func (m *Model) Press(y int, models []protocol.VaultEntry) tea.Cmd {
	_, rowAt := m.body(models)
	line := y - 1
	if line < 0 || line >= len(rowAt) || rowAt[line] < 0 {
		return nil
	}
	m.cursor = rowAt[line]
	return choose(m.visible(models)[m.cursor])
}

func choose(e protocol.VaultEntry) tea.Cmd {
	if e.Present {
		return emit(PickMsg{ModelKey: e.ModelKey})
	}
	return emit(OpenVaultMsg{})
}

func (m Model) View(models []protocol.VaultEntry) string {
	lines, _ := m.body(models)
	return lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(m.theme.Edge).
		Background(m.theme.Background).
		Padding(0, 1).
		Width(boxWidth).
		Render(strings.Join(lines, "\n"))
}

func (m Model) body(models []protocol.VaultEntry) ([]string, []int) {
	th := m.theme
	inner := boxWidth - 4
	rows := m.visible(models)

	var lines []string
	var rowAt []int
	add := func(s string, row int) {
		for _, l := range strings.Split(s, "\n") {
			lines = append(lines, l)
			rowAt = append(rowAt, row)
		}
	}
	for _, s := range []string{
		spread(th.Accent.Bold(true).Render("model"), th.Muted.Render("esc close"), inner),
		th.Box(true).Width(inner).Render(m.filter.View()),
	} {
		add(s, -1)
	}
	if len(models) == 0 {
		add(th.Muted.Render("loading models…"), -1)
	}
	shownLocked := false
	for i, e := range rows {
		if i == 0 && e.Present {
			add(th.Heading.Render("ready"), -1)
		}
		if !e.Present && !shownLocked {
			shownLocked = true
			add(th.Heading.Render("no key"), -1)
		}
		add(m.row(e, i == m.cursor, inner), i)
	}
	if len(models) > 0 && len(rows) == 0 {
		add(th.Muted.Render("no match"), -1)
	}
	if m.note != "" {
		add(th.Rule.Render(strings.Repeat("─", inner)), -1)
		add(th.Muted.Render(m.note), -1)
	}
	return lines, rowAt
}

func (m Model) row(e protocol.VaultEntry, selected bool, width int) string {
	th := m.theme
	cursor := "  "
	if selected {
		cursor = th.Accent.Render("▸ ")
	}
	name := th.Text.Render(e.ModelKey)
	right := th.Muted.Render(e.Price)
	switch {
	case !e.Present:
		name = th.Muted.Render(e.ModelKey)
		right = th.Accent.Render("add key in vault →")
	case e.Validated != nil && !*e.Validated:
		right = th.Warn.Render("key failed")
	}
	if e.ModelKey == m.current {
		name += th.Accent.Render(" ●")
	}
	line := spread(cursor+name, right, width)
	if selected {
		return lipgloss.NewStyle().Background(th.Selection).Width(width).Render(line)
	}
	return line
}

func spread(left, right string, width int) string {
	gap := max(width-lipgloss.Width(left)-lipgloss.Width(right), 1)
	return left + strings.Repeat(" ", gap) + right
}

func emit(msg tea.Msg) tea.Cmd {
	return func() tea.Msg { return msg }
}
