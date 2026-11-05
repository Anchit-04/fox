package sidebar

import (
	"fmt"
	"image/color"
	"strings"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"

	"fox/tui/internal/state"
	"fox/tui/internal/ui/theme"
)

type SelectMsg struct {
	SessionID string
	TaskID    string
}

type NewChatMsg struct {
	Workspace string
	Name      string
}

type NewProjectMsg struct{}

type OpenVaultMsg struct{}

type PopoutMsg struct {
	Label      string
	SessionIDs []string
}

type rowKind int

const (
	projectRow rowKind = iota
	sessionRow
	taskRow
)

type row struct {
	kind     rowKind
	project  state.Project
	session  *state.Session
	task     *state.Task
	depth    int
	last     bool
	sessions []string
}

type drag struct {
	row            row
	startX, startY int
	x, y           int
	moving         bool
}

const (
	actionsRow    = 0
	newChatLabel  = "+ new chat"
	newProjLabel  = "+ new project"
	sharedMark    = "⇄"
	listTop       = 5
	footerRows    = 3
	dragThreshold = 2
	titleWords    = 6
)

type Model struct {
	theme     *theme.Theme
	collapsed map[string]bool
	rows      []row
	cursor    int
	offset    int
	active    string
	task      string
	width     int
	height    int
	keys      int
	models    int
	drag      *drag
}

func New(th *theme.Theme) Model {
	return Model{theme: th, collapsed: map[string]bool{}}
}

func (m *Model) SetSize(w, h int) {
	m.width, m.height = w, h
	m.keepVisible()
}

func (m *Model) Refresh(s *state.Store, active, task string) {
	m.rows = m.rows[:0]
	for _, p := range s.Projects() {
		var ids []string
		for _, sess := range p.Sessions {
			ids = append(ids, sess.ID)
			for _, child := range s.Children(sess.ID) {
				ids = append(ids, child.ID)
			}
		}
		m.rows = append(m.rows, row{kind: projectRow, project: p, sessions: ids})
		if m.collapsed[p.Path] {
			continue
		}
		for _, sess := range p.Sessions {
			m.rows = append(m.rows, row{kind: sessionRow, project: p, session: sess, depth: 1, sessions: []string{sess.ID}})
			children := s.Children(sess.ID)
			for i, id := range sess.TaskOrder {
				last := i == len(sess.TaskOrder)-1 && len(children) == 0
				m.rows = append(m.rows, row{kind: taskRow, project: p, session: sess, task: sess.Tasks[id], depth: 2, last: last, sessions: []string{sess.ID}})
			}
			for i, child := range children {
				m.rows = append(m.rows, row{kind: sessionRow, project: p, session: child, depth: 2, last: i == len(children)-1, sessions: []string{child.ID}})
			}
		}
	}

	m.keys, m.models = 0, len(s.Models)
	for _, e := range s.Models {
		if e.Present {
			m.keys++
		}
	}

	if active != m.active || task != m.task {
		m.active, m.task = active, task
		m.cursor = m.activeRow()
	}
	m.cursor = min(max(m.cursor, 0), max(len(m.rows)-1, 0))
	m.keepVisible()
}

func (m *Model) Project() (state.Project, bool) {
	if m.cursor < 0 || m.cursor >= len(m.rows) {
		return state.Project{}, false
	}
	p := m.rows[m.cursor].project
	return p, p.Name != ""
}

func (m *Model) newChat() tea.Cmd {
	p, _ := m.Project()
	return emit(NewChatMsg{Workspace: p.Path, Name: p.Name})
}

func (m *Model) activeRow() int {
	for i, r := range m.rows {
		if m.isActive(r) {
			return i
		}
	}
	return m.cursor
}

func (m *Model) isActive(r row) bool {
	switch r.kind {
	case sessionRow:
		return r.session.ID == m.active && m.task == ""
	case taskRow:
		return r.session.ID == m.active && r.task.ID == m.task
	}
	return false
}

func (m *Model) listHeight() int {
	return max(m.height-listTop-footerRows, 1)
}

func (m *Model) keepVisible() {
	h := m.listHeight()
	if m.cursor < m.offset {
		m.offset = m.cursor
	}
	if m.cursor >= m.offset+h {
		m.offset = m.cursor - h + 1
	}
	m.offset = min(max(m.offset, 0), max(len(m.rows)-h, 0))
}

func (m *Model) Key(msg tea.KeyPressMsg) tea.Cmd {
	switch msg.String() {
	case "n":
		return m.newChat()
	case "p":
		return emit(NewProjectMsg{})
	}
	if len(m.rows) == 0 {
		return nil
	}
	switch msg.String() {
	case "up", "k":
		m.cursor = max(m.cursor-1, 0)
	case "down", "j":
		m.cursor = min(m.cursor+1, len(m.rows)-1)
	case "left", "h":
		if r := m.rows[m.cursor]; r.kind == projectRow {
			m.collapsed[r.project.Path] = true
		}
	case "right", "l":
		if r := m.rows[m.cursor]; r.kind == projectRow {
			delete(m.collapsed, r.project.Path)
		}
	case "enter", "space":
		return m.activate(m.rows[m.cursor])
	}
	m.keepVisible()
	return nil
}

func (m *Model) Selected() (PopoutMsg, bool) {
	if m.cursor < 0 || m.cursor >= len(m.rows) {
		return PopoutMsg{}, false
	}
	return m.popout(m.rows[m.cursor])
}

func (m *Model) activate(r row) tea.Cmd {
	switch r.kind {
	case projectRow:
		m.collapsed[r.project.Path] = !m.collapsed[r.project.Path]
		return nil
	case taskRow:
		return emit(SelectMsg{SessionID: r.session.ID, TaskID: r.task.ID})
	default:
		return emit(SelectMsg{SessionID: r.session.ID})
	}
}

func (m *Model) popout(r row) (PopoutMsg, bool) {
	if len(r.sessions) == 0 {
		return PopoutMsg{}, false
	}
	return PopoutMsg{Label: m.label(r), SessionIDs: r.sessions}, true
}

func (m *Model) rowAt(y int) (int, bool) {
	if y < listTop || y >= listTop+m.listHeight() {
		return 0, false
	}
	i := m.offset + y - listTop
	return i, i < len(m.rows)
}

func (m *Model) Press(x, y int, button tea.MouseButton) tea.Cmd {
	if y == m.height-2 {
		return emit(OpenVaultMsg{})
	}
	if y == actionsRow {
		if x < lipgloss.Width(newChatLabel)+1 {
			return m.newChat()
		}
		if x < lipgloss.Width(newChatLabel)+2+lipgloss.Width(newProjLabel)+1 {
			return emit(NewProjectMsg{})
		}
		return nil
	}
	i, ok := m.rowAt(y)
	if !ok {
		return nil
	}
	m.cursor = i
	r := m.rows[i]
	if r.kind == projectRow && button == tea.MouseLeft && x >= m.width-2 {
		return emit(NewChatMsg{Workspace: r.project.Path, Name: r.project.Name})
	}
	if button == tea.MouseRight {
		if msg, ok := m.popout(r); ok {
			return emit(msg)
		}
		return nil
	}
	m.drag = &drag{row: r, startX: x, startY: y, x: x, y: y}
	return nil
}

func (m *Model) Motion(x, y int) {
	if m.drag == nil {
		return
	}
	m.drag.x, m.drag.y = x, y
	if abs(x-m.drag.startX)+abs(y-m.drag.startY) >= dragThreshold {
		m.drag.moving = true
	}
}

func (m *Model) Release(inDropZone bool) tea.Cmd {
	d := m.drag
	m.drag = nil
	if d == nil {
		return nil
	}
	if !d.moving {
		return m.activate(d.row)
	}
	if inDropZone {
		if msg, ok := m.popout(d.row); ok {
			return emit(msg)
		}
	}
	return nil
}

func (m *Model) Dragging() bool {
	return m.drag != nil && m.drag.moving
}

func (m *Model) Ghost() (string, int, int) {
	if !m.Dragging() {
		return "", 0, 0
	}
	return m.label(m.drag.row), m.drag.x, m.drag.y
}

func (m *Model) Scroll(delta int) {
	m.offset = min(max(m.offset+delta, 0), max(len(m.rows)-m.listHeight(), 0))
}

func (m *Model) label(r row) string {
	switch r.kind {
	case projectRow:
		return r.project.Name
	default:
		return Title(r.session)
	}
}

func Title(sess *state.Session) string {
	if sess == nil || strings.TrimSpace(sess.Task) == "" {
		return "new chat"
	}
	first, _, _ := strings.Cut(strings.TrimSpace(sess.Task), "\n")
	words := strings.Fields(first)
	if len(words) > titleWords {
		return strings.Join(words[:titleWords], " ") + "…"
	}
	return strings.Join(words, " ")
}

func (m Model) View(focused bool) string {
	th := m.theme
	w := max(m.width, 1)
	search := th.Box(false).Width(w).Render(spread(th.Faint.Render("⌕ search"), th.Faint.Render("soon"), w-4))
	actions := th.Accent.Render(newChatLabel) + "  " + th.Text.Render(newProjLabel)
	if lipgloss.Width(newChatLabel+"  "+newProjLabel) > w {
		actions = th.Accent.Render(fit(newChatLabel+"  "+newProjLabel, w))
	}
	lines := append([]string{actions}, strings.Split(search, "\n")...)
	lines = append(lines, th.Heading.Render("projects"))

	h := m.listHeight()
	for i := m.offset; i < min(m.offset+h, len(m.rows)); i++ {
		lines = append(lines, m.renderRow(m.rows[i], i == m.cursor && focused))
	}
	for len(lines) < listTop+h {
		lines = append(lines, "")
	}

	vault := spread(th.Text.Render("⚿ vault"), th.Muted.Render(fmt.Sprintf("%d of %d keys", m.keys, m.models)), w)
	legend := th.Muted.Render(fit("● run ! you ○ idle ✓ done "+sharedMark+" shared", w))
	lines = append(lines, th.Rule.Render(strings.Repeat("─", w)), vault, legend)
	return lipgloss.NewStyle().Width(w).Height(m.height).MaxHeight(m.height).Render(strings.Join(lines, "\n"))
}

func (m Model) renderRow(r row, cursor bool) string {
	th := m.theme
	selected := m.isActive(r)
	var bg color.Color
	if selected {
		bg = th.Selection
	}
	paint := func(st lipgloss.Style, s string) string {
		if bg != nil {
			st = st.Background(bg)
		}
		return st.Render(s)
	}

	marker := " "
	markStyle := th.Muted
	switch {
	case selected:
		marker, markStyle = "▎", th.Accent
	case cursor:
		marker, markStyle = "›", th.Accent
	}

	var icon, text, mark, right string
	iconStyle, textStyle, rightStyle := th.Muted, th.Text, th.Muted
	indent := strings.Repeat("  ", r.depth)
	if r.depth == 2 {
		indent = "  " + branch(r.last)
	}

	switch r.kind {
	case projectRow:
		icon, textStyle = "▾", th.Strong
		if m.collapsed[r.project.Path] {
			icon = "▸"
		}
		iconStyle = th.Strong
		text = r.project.Name
		right = fmt.Sprint(len(r.project.Sessions)) + " +"
		if n := needsYou(r.project); n > 0 {
			right, rightStyle = fmt.Sprintf("%d! +", n), th.Warn
		}
	case sessionRow:
		icon, iconStyle = statusIcon(th, r.session.Status)
		text = Title(r.session)
		right, rightStyle = statusWord(th, r.session)
		if r.session.SharedMemory {
			mark = sharedMark
		}
	case taskRow:
		icon, iconStyle = taskIcon(th, r.task.Status)
		text, textStyle = "exec · "+taskLabel(r.task), th.Muted
		if selected {
			textStyle = th.Text
		}
	}
	if mark != "" && right != "" {
		mark += " "
	}

	left := 1 + lipgloss.Width(indent) + lipgloss.Width(icon) + 1
	tail := lipgloss.Width(mark) + lipgloss.Width(right)
	room := max(m.width-left-tail-1, 1)
	text = fit(text, room)
	gap := max(m.width-left-lipgloss.Width(text)-tail, 1)

	return paint(markStyle, marker) + paint(th.Muted, indent) + paint(iconStyle, icon) + paint(th.Text, " ") +
		paint(textStyle, text) + paint(th.Text, strings.Repeat(" ", gap)) + paint(th.Accent, mark) + paint(rightStyle, right)
}

func statusIcon(th *theme.Theme, st state.Status) (string, lipgloss.Style) {
	switch st {
	case state.Working:
		return "●", th.Accent
	case state.NeedsYou:
		return "!", th.Warn
	case state.Idle:
		return "○", th.Text
	case state.Done:
		return "✓", th.Muted
	default:
		return "✗", th.Warn
	}
}

func statusWord(th *theme.Theme, sess *state.Session) (string, lipgloss.Style) {
	switch sess.Status {
	case state.NeedsYou:
		return "needs you", th.Warn
	case state.Idle:
		if sess.Unread {
			return "•", th.Accent
		}
		return "idle", th.Muted
	case state.Failed:
		return "failed", th.Warn
	case state.Working:
		return "", th.Muted
	}
	if sess.Unread {
		return "•", th.Accent
	}
	return "", th.Muted
}

func taskIcon(th *theme.Theme, status string) (string, lipgloss.Style) {
	switch status {
	case "running":
		return "◐", th.Accent
	case "needs-approval":
		return "!", th.Warn
	case "done":
		return "✓", th.Muted
	case "failed":
		return "✗", th.Warn
	case "cancelled":
		return "–", th.Muted
	case "escalated":
		return "↑", th.Accent
	default:
		return "·", th.Muted
	}
}

func taskLabel(t *state.Task) string {
	if len(t.Scope) > 0 {
		return t.Scope[0]
	}
	if t.Description != "" {
		return t.Description
	}
	return t.ID
}

func needsYou(p state.Project) int {
	n := 0
	for _, sess := range p.Sessions {
		if sess.Status == state.NeedsYou {
			n++
		}
	}
	return n
}

func branch(last bool) string {
	if last {
		return "└ "
	}
	return "├ "
}

func spread(left, right string, width int) string {
	gap := max(width-lipgloss.Width(left)-lipgloss.Width(right), 1)
	return left + strings.Repeat(" ", gap) + right
}

func fit(s string, width int) string {
	if lipgloss.Width(s) <= width {
		return s
	}
	runes := []rune(s)
	for len(runes) > 0 && lipgloss.Width(string(runes))+1 > width {
		runes = runes[:len(runes)-1]
	}
	return string(runes) + "…"
}

func emit(msg tea.Msg) tea.Cmd {
	return func() tea.Msg { return msg }
}

func abs(n int) int {
	if n < 0 {
		return -n
	}
	return n
}
