package app

import (
	"strings"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"

	"fox/tui/internal/protocol"
	"fox/tui/internal/ui/diffview"
)

func (m *Model) afterEvent(ev protocol.Event) tea.Cmd {
	switch ev.Type {
	case protocol.FileDiffs:
		p, err := protocol.Payload[protocol.FileDiffPayload](ev)
		if err != nil {
			return nil
		}
		if p.Source == diffview.Fox {
			m.changed[ev.SessionID] = len(p.Files)
		}
		if m.diffOpen && ev.SessionID == m.diff.Session() && p.Source == m.diff.Source() {
			m.diff.Set(p)
		}
	case protocol.ToolResult:
		if ev.SessionID != m.active {
			return nil
		}
		p, err := protocol.Payload[protocol.ToolResultPayload](ev)
		if err != nil {
			return nil
		}
		gitView := m.diffOpen && m.diff.Source() == diffview.Git
		switch p.Name {
		case "write_file", "edit_file":
			if gitView {
				return tea.Batch(m.requestDiff(diffview.Fox), m.requestDiff(diffview.Git))
			}
			return m.requestDiff(diffview.Fox)
		case "run_bash_command":
			if gitView {
				return m.requestDiff(diffview.Git)
			}
		}
	}
	return nil
}

func (m *Model) requestDiff(source string) tea.Cmd {
	if m.active == "" {
		return nil
	}
	return m.send.SendCmd(protocol.FileDiff{SessionID: m.active, Source: source})
}

func (m *Model) followDiff() tea.Cmd {
	if m.active == "" {
		return nil
	}
	count := m.requestDiff(diffview.Fox)
	if !m.diffOpen {
		return count
	}
	m.diff.Open(m.active)
	if m.diff.Source() == diffview.Git {
		return tea.Batch(count, m.requestDiff(diffview.Git))
	}
	return count
}

func (m *Model) toggleDiff() tea.Cmd {
	if m.diffOpen {
		return m.closeDiff()
	}
	if m.active == "" {
		m.flash = "start a chat first · ctrl+d then shows the files it changed"
		return nil
	}
	m.diffOpen = true
	m.diff.Open(m.active)
	return tea.Batch(m.setFocus(focusDiff), m.requestDiff(m.diff.Source()))
}

func (m *Model) closeDiff() tea.Cmd {
	m.diffOpen = false
	if m.focus == focusDiff {
		return m.setFocus(focusComposer)
	}
	return nil
}

func (m *Model) blurDiff() tea.Cmd {
	if m.frame.DiffFull {
		return m.closeDiff()
	}
	return m.setFocus(focusComposer)
}

func (m Model) nextFocus() focus {
	switch m.focus {
	case focusComposer:
		if m.frame.HasSidebar {
			return focusSidebar
		}
		if m.diffOpen {
			return focusDiff
		}
	case focusSidebar:
		if m.diffOpen {
			return focusDiff
		}
	}
	return focusComposer
}

func (m Model) withDiff(main string) string {
	f := m.frame
	if !f.HasDiff {
		return main
	}
	pane := lipgloss.NewStyle().Width(f.Diff.W).Height(f.Diff.H).MaxWidth(f.Diff.W).MaxHeight(f.Diff.H).
		Render(m.diff.View(m.focus == focusDiff))
	if f.DiffFull {
		return pane
	}
	main = lipgloss.NewStyle().Width(f.Main.W).Height(f.Main.H).MaxWidth(f.Main.W).MaxHeight(f.Main.H).Render(main)
	rule := m.theme.Rule
	if m.focus == focusDiff {
		rule = m.theme.Accent
	}
	divider := rule.Render(strings.TrimSuffix(strings.Repeat("│\n", f.DiffDivider.H), "\n"))
	return lipgloss.JoinHorizontal(lipgloss.Top, main, divider, pane)
}
