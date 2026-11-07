package app

import (
	"strings"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"

	"fox/tui/internal/protocol"
	"fox/tui/internal/ui/board"
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
		case board.Tool:
			m.boardDrawn(p.Content)
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
		return m.closeSide()
	}
	if m.active == "" {
		m.flash = "start a chat first · ctrl+d then shows the files it changed"
		return nil
	}
	m.diffOpen, m.boardOpen = true, false
	m.diff.Open(m.active)
	return tea.Batch(m.setFocus(focusSide), m.requestDiff(m.diff.Source()))
}

func (m *Model) toggleBoard() tea.Cmd {
	if m.boardOpen {
		return m.closeSide()
	}
	if m.active == "" {
		m.flash = "start a chat first · ctrl+b then shows what fox draws"
		return nil
	}
	m.boardOpen, m.diffOpen = true, false
	m.board.Follow()
	return m.setFocus(focusSide)
}

func (m *Model) boardDrawn(result string) {
	if strings.HasPrefix(strings.TrimSpace(result), `{"error"`) {
		return
	}
	switch {
	case m.boardOpen:
		m.board.Follow()
	case m.diffOpen:
		m.flash = "fox drew on the board · ctrl+b shows it"
	default:
		m.boardOpen = true
		m.board.Follow()
	}
}

func (m Model) sideOpen() bool {
	return m.diffOpen || m.boardOpen
}

func (m *Model) closeSide() tea.Cmd {
	m.diffOpen, m.boardOpen = false, false
	if m.focus == focusSide {
		return m.setFocus(focusComposer)
	}
	return nil
}

func (m *Model) blurSide() tea.Cmd {
	if m.frame.SideFull {
		return m.closeSide()
	}
	return m.setFocus(focusComposer)
}

func (m Model) nextFocus() focus {
	switch m.focus {
	case focusComposer:
		if m.frame.HasSidebar {
			return focusSidebar
		}
		if m.sideOpen() {
			return focusSide
		}
	case focusSidebar:
		if m.sideOpen() {
			return focusSide
		}
	}
	return focusComposer
}

func (m *Model) withSide(main string) string {
	f := m.frame
	if !f.HasSide {
		return main
	}
	content := ""
	if m.boardOpen {
		content = m.board.View(board.Cards(m.store.Session(m.active)), m.focus == focusSide)
	} else {
		content = m.diff.View(m.focus == focusSide)
	}
	pane := lipgloss.NewStyle().Width(f.Side.W).Height(f.Side.H).MaxWidth(f.Side.W).MaxHeight(f.Side.H).
		Render(content)
	if f.SideFull {
		return pane
	}
	main = lipgloss.NewStyle().Width(f.Main.W).Height(f.Main.H).MaxWidth(f.Main.W).MaxHeight(f.Main.H).Render(main)
	rule := m.theme.Rule
	if m.focus == focusSide {
		rule = m.theme.Accent
	}
	divider := rule.Render(strings.TrimSuffix(strings.Repeat("│\n", f.SideDivider.H), "\n"))
	return lipgloss.JoinHorizontal(lipgloss.Top, main, divider, pane)
}
