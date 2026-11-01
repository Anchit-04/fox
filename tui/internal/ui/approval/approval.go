package approval

import (
	"fmt"
	"strings"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"

	"fox/tui/internal/state"
	"fox/tui/internal/ui/theme"
)

type DecisionMsg struct {
	Approval state.Approval
	Approved bool
	Always   bool
}

const maxWidth = 72

func Update(msg tea.KeyPressMsg, a state.Approval) tea.Cmd {
	var d DecisionMsg
	switch msg.String() {
	case "y":
		d = DecisionMsg{Approval: a, Approved: true}
	case "a":
		d = DecisionMsg{Approval: a, Approved: true, Always: true}
	case "n", "esc":
		d = DecisionMsg{Approval: a}
	default:
		return nil
	}
	return func() tea.Msg { return d }
}

func View(th *theme.Theme, a state.Approval, where string, waiting, width int) string {
	w := min(maxWidth, max(width-4, 24))
	inner := w - 6
	wrap := lipgloss.NewStyle().Width(inner)

	what := a.Description
	if what == "" {
		what = a.ToolName
	}
	lines := []string{
		spread(th.Warn.Bold(true).Render("fox wants to"), th.Muted.Render(where), inner),
		"",
		wrap.Inherit(th.Text).Render(what),
	}
	if a.Reason != "" {
		lines = append(lines, wrap.Inherit(th.Muted).Render("why ask: "+a.Reason))
	}
	lines = append(lines, "",
		th.Text.Render("[y] allow   [n] deny   [a] always allow "+a.ToolName))
	if waiting > 0 {
		lines = append(lines, th.Muted.Render(fmt.Sprintf("%d more waiting", waiting)))
	}
	return lipgloss.NewStyle().
		Border(lipgloss.DoubleBorder()).
		BorderForeground(th.Alert).
		Background(th.Background).
		Padding(1, 2).
		Width(w).
		Render(strings.Join(lines, "\n"))
}

func spread(left, right string, width int) string {
	gap := max(width-lipgloss.Width(left)-lipgloss.Width(right), 1)
	return left + strings.Repeat(" ", gap) + right
}
