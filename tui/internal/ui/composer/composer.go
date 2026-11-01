package composer

import (
	"strings"

	"charm.land/bubbles/v2/key"
	"charm.land/bubbles/v2/textarea"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"

	"fox/tui/internal/ui/theme"
)

type SubmitMsg struct {
	Text string
}

type OpenPickerMsg struct{}

type ToggleModeMsg struct{}

const (
	minLines = 1
	maxLines = 6
	chrome   = 3
	chipGap  = 2
)

type Model struct {
	theme *theme.Theme
	input textarea.Model
	width int
	model string
	mode  string
}

func New(th *theme.Theme) Model {
	ta := textarea.New()
	ta.Prompt = ""
	ta.ShowLineNumbers = false
	ta.SetHeight(minLines)
	ta.KeyMap.InsertNewline = key.NewBinding(key.WithKeys("alt+enter", "shift+enter"))
	st := ta.Styles()
	st.Focused.Text = th.Text
	st.Blurred.Text = th.Text
	st.Focused.Placeholder = th.Muted
	st.Blurred.Placeholder = th.Muted
	st.Focused.CursorLine = lipgloss.NewStyle()
	st.Blurred.CursorLine = lipgloss.NewStyle()
	st.Cursor.Color = th.Ink
	ta.SetStyles(st)
	ta.Focus()
	return Model{theme: th, input: ta, model: "auto", mode: "agent"}
}

func (m *Model) SetWidth(w int) {
	m.width = w
	m.input.SetWidth(max(w-4, 1))
	m.fit()
}

func (m *Model) SetModel(name string) {
	if name == "" {
		name = "auto"
	}
	m.model = name
}

func (m *Model) SetMode(mode string) {
	m.mode = mode
}

func (m *Model) SetPlaceholder(s string) {
	m.input.Placeholder = s
}

func (m *Model) Focus() tea.Cmd {
	return m.input.Focus()
}

func (m *Model) Blur() {
	m.input.Blur()
}

func (m Model) Focused() bool {
	return m.input.Focused()
}

func (m Model) Height() int {
	return m.input.Height() + chrome
}

func (m Model) Value() string {
	return m.input.Value()
}

func (m *Model) SetValue(s string) {
	m.input.SetValue(s)
	m.fit()
}

func (m *Model) Reset() {
	m.input.Reset()
	m.fit()
}

func (m *Model) Update(msg tea.Msg) tea.Cmd {
	if k, ok := msg.(tea.KeyPressMsg); ok && k.String() == "enter" {
		text := strings.TrimSpace(m.input.Value())
		if text == "" {
			return nil
		}
		m.Reset()
		return func() tea.Msg { return SubmitMsg{Text: text} }
	}
	var cmd tea.Cmd
	m.input, cmd = m.input.Update(msg)
	m.fit()
	return cmd
}

func (m *Model) Press(x, y int) tea.Cmd {
	if y == m.Height()-1 {
		modelW, modeW := lipgloss.Width(m.chipView()), lipgloss.Width(m.modeView())
		switch {
		case x >= m.width-modelW:
			return func() tea.Msg { return OpenPickerMsg{} }
		case x >= m.width-modelW-chipGap-modeW && x < m.width-modelW-chipGap:
			return func() tea.Msg { return ToggleModeMsg{} }
		}
	}
	return m.Focus()
}

func (m *Model) fit() {
	rows := displayRows(m.input.Value(), max(m.input.Width(), 1))
	m.input.SetHeight(min(max(rows, minLines), maxLines))
}

func (m Model) chipView() string {
	th := m.theme
	return th.Accent.Render("▸ ") + th.Text.Render(m.model) + th.Muted.Render(" ▾")
}

func (m Model) modeView() string {
	return m.theme.Muted.Render(m.mode)
}

func (m Model) View(hint string) string {
	th := m.theme
	box := th.Box(m.input.Focused()).Width(m.width).Render(m.input.View())
	chip := m.modeView() + strings.Repeat(" ", chipGap) + m.chipView()
	chipW := lipgloss.Width(chip)
	left := th.Muted.Render(clip(hint, max(m.width-chipW-2, 0)))
	gap := max(m.width-lipgloss.Width(left)-chipW, 1)
	return box + "\n" + left + strings.Repeat(" ", gap) + chip
}

func displayRows(s string, width int) int {
	rows := 0
	for _, line := range strings.Split(s, "\n") {
		rows += max((lipgloss.Width(line)+width)/width, 1)
	}
	return rows
}

func clip(s string, width int) string {
	if lipgloss.Width(s) <= width {
		return s
	}
	runes := []rune(s)
	for len(runes) > 0 && lipgloss.Width(string(runes))+1 > width {
		runes = runes[:len(runes)-1]
	}
	return string(runes) + "…"
}
