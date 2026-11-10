package vault

import (
	"strings"

	"charm.land/bubbles/v2/textinput"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"

	"fox/tui/internal/protocol"
	"fox/tui/internal/ui/theme"
)

type SaveMsg struct {
	ModelKey string
	Value    string
}

type ValidateMsg struct{}

type CloseMsg struct{}

const (
	listWidth = 30
	listTop   = 2
)

type Model struct {
	theme   *theme.Theme
	input   textinput.Model
	cursor  int
	editing bool
	busy    bool
	status  string
}

func New(th *theme.Theme) Model {
	ti := textinput.New()
	ti.EchoMode = textinput.EchoPassword
	ti.Prompt = ""
	ti.Placeholder = "paste key"
	ti.SetWidth(40)
	return Model{theme: th, input: ti}
}

func (m *Model) Open() {
	m.editing, m.status = false, ""
	m.input.Reset()
	m.input.Blur()
}

func (m *Model) SetBusy(busy bool, status string) {
	m.busy, m.status = busy, status
}

func (m Model) Editing() bool {
	return m.editing
}

func (m *Model) Update(msg tea.Msg, models []protocol.VaultEntry) tea.Cmd {
	m.cursor = min(m.cursor, max(len(models)-1, 0))
	k, isKey := msg.(tea.KeyPressMsg)
	if m.editing {
		if isKey {
			switch k.String() {
			case "esc":
				m.editing = false
				m.input.Reset()
				m.input.Blur()
				return nil
			case "enter":
				value := strings.TrimSpace(m.input.Value())
				m.editing = false
				m.input.Reset()
				m.input.Blur()
				if value == "" || m.cursor >= len(models) {
					return nil
				}
				return emit(SaveMsg{ModelKey: models[m.cursor].ModelKey, Value: value})
			}
		}
		var cmd tea.Cmd
		m.input, cmd = m.input.Update(msg)
		return cmd
	}
	if !isKey {
		return nil
	}
	switch k.String() {
	case "esc", "q":
		return emit(CloseMsg{})
	case "up", "k":
		m.cursor = max(m.cursor-1, 0)
	case "down", "j":
		m.cursor = min(m.cursor+1, max(len(models)-1, 0))
	case "enter":
		if m.cursor < len(models) {
			m.editing = true
			return m.input.Focus()
		}
	case "v":
		return emit(ValidateMsg{})
	}
	return nil
}

func (m *Model) Press(x, y int, models []protocol.VaultEntry) tea.Cmd {
	if x >= listWidth {
		return nil
	}
	if i := y - listTop; i >= 0 && i < len(models) {
		m.cursor = i
	}
	return nil
}

func (m Model) View(models []protocol.VaultEntry, width, height int) string {
	th := m.theme
	list := []string{th.Accent.Bold(true).Render("⚿ vault"), ""}
	for i, e := range models {
		list = append(list, m.row(e, i == m.cursor))
	}
	if len(models) == 0 {
		list = append(list, th.Muted.Render("loading…"))
	}
	left := lipgloss.NewStyle().Width(listWidth).Height(height).Padding(0, 1).Render(strings.Join(list, "\n"))
	divider := th.Rule.Render(strings.TrimSuffix(strings.Repeat("│\n", height), "\n"))
	right := lipgloss.NewStyle().Width(max(width-listWidth-1, 1)).Height(height).Padding(0, 2).
		Render(m.detail(models, max(width-listWidth-5, 10)))
	return lipgloss.JoinHorizontal(lipgloss.Top, left, divider, right)
}

func (m Model) row(e protocol.VaultEntry, selected bool) string {
	th := m.theme
	paint := func(st lipgloss.Style, text string) string {
		if selected {
			st = st.Background(th.Selection)
		}
		return st.Render(text)
	}
	marker := paint(th.Text, " ")
	if selected {
		marker = paint(th.Accent, "▎")
	}
	nameStyle := th.Text
	if !e.Present {
		nameStyle = th.Muted
	}
	mark, markStyle := keyState(th, e)
	gap := max(listWidth-3-lipgloss.Width(e.ModelKey)-lipgloss.Width(mark), 1)
	return marker + paint(nameStyle, e.ModelKey) + paint(th.Text, strings.Repeat(" ", gap)) + paint(markStyle, mark)
}

func keyState(th *theme.Theme, e protocol.VaultEntry) (string, lipgloss.Style) {
	switch {
	case !e.Present:
		return "no key", th.Muted
	case e.Validated == nil:
		return "? unchecked", th.Muted
	case *e.Validated:
		return "✓", th.Accent
	default:
		return "✗ failed", th.Warn
	}
}

func (m Model) detail(models []protocol.VaultEntry, width int) string {
	th := m.theme
	if m.cursor >= len(models) {
		return th.Muted.Render("no models registered")
	}
	e := models[m.cursor]
	lines := []string{
		"",
		th.Strong.Render(e.ModelKey),
		th.Muted.Render(e.Price + " · stored in FOX_HOME/.env, never shown again"),
		"",
	}
	field := th.Muted.Render("press enter to set a key")
	if m.editing {
		field = m.input.View()
	} else if e.Present {
		field = th.Muted.Render("•••••••••••••••• (enter to replace)")
	}
	lines = append(lines, th.Box(m.editing).Width(min(width, 52)).Render(th.Accent.Render("key ▸ ")+field), "")

	hint := "enter set key · v validate all · esc back"
	if m.editing {
		hint = "enter save and validate · esc cancel"
	}
	mark, markStyle := keyState(th, e)
	lines = append(lines, th.Muted.Render(hint), "", markStyle.Render(mark)+th.Muted.Render(" "+stateWord(e)))
	if m.status != "" {
		lines = append(lines, "", th.Muted.Render(m.status))
	}
	return strings.Join(lines, "\n")
}

func stateWord(e protocol.VaultEntry) string {
	switch {
	case !e.Present:
		return "no key saved for this model"
	case e.Validated == nil:
		return "key present, not checked yet (v checks every key with its provider)"
	case *e.Validated:
		return "key valid"
	default:
		return "the provider rejected this key"
	}
}

func spread(left, right string, width int) string {
	gap := max(width-lipgloss.Width(left)-lipgloss.Width(right), 1)
	return left + strings.Repeat(" ", gap) + right
}

func emit(msg tea.Msg) tea.Cmd {
	return func() tea.Msg { return msg }
}
