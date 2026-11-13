package sidebar

import (
	"fmt"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"

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

type DeleteMsg struct {
	SessionID string
	Label     string
}

type ArchiveMsg struct {
	SessionID string
	Archived  bool
}

type CleanMsg struct {
	Workspace string
	Name      string
}

type ToggleArchivedMsg struct{}

type PopoutMsg struct {
	Label      string
	SessionIDs []string
}

type rowKind int

const (
	projectRow rowKind = iota
	sessionRow
	taskRow
	headRow
	agentRow
	agentTaskRow
	gapRow
)

func (k rowKind) selectable() bool { return k != headRow && k != gapRow }

type Tab int

const (
	ProjectsTab Tab = iota
	ChatTab
)

type row struct {
	kind     rowKind
	project  state.Project
	session  *state.Session
	task     *state.Task
	depth    int
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
	projectsLabel = " Projects "
	chatLabel     = " Chat "
	addLabel      = "+ "
	sharedMark    = "⇄"
	listTop       = 2
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
	tab       Tab
	agents    int
	retarget  bool
}

func (m *Model) Tab() Tab { return m.tab }

func (m *Model) SetTab(t Tab) {
	if m.tab != t {
		m.tab, m.offset, m.retarget = t, 0, true
	}
}

func (m Model) tabLabel(t Tab) string {
	if t == ProjectsTab {
		return projectsLabel
	}
	if m.agents > 0 {
		return fmt.Sprintf("%s%d ", chatLabel, m.agents)
	}
	return chatLabel
}

func running(sess *state.Session) bool {
	return sess.Status == state.Working || sess.Status == state.NeedsYou
}

func agentRows(s *state.Store) ([]row, int) {
	var rows []row
	count := 0
	for _, p := range s.Projects() {
		var block []row
		for _, top := range p.Sessions {
			for _, sess := range append([]*state.Session{top}, s.Children(top.ID)...) {
				var tasks []row
				for _, id := range sess.TaskOrder {
					if t := sess.Tasks[id]; t.Live() {
						tasks = append(tasks, row{kind: agentTaskRow, project: p, session: sess, task: t, depth: 2, sessions: []string{sess.ID}})
					}
				}
				if running(sess) || len(tasks) > 0 {
					block = append(block, row{kind: agentRow, project: p, session: sess, sessions: []string{sess.ID}})
					block = append(block, tasks...)
				}
			}
		}
		if len(block) == 0 {
			continue
		}
		count += len(block)
		rows = append(rows, row{kind: headRow, project: p})
		for _, r := range block {
			rows = append(rows, r, row{kind: gapRow, project: p})
		}
	}
	return rows, count
}

func (m *Model) step(from, dir int) int {
	for i := from + dir; i >= 0 && i < len(m.rows); i += dir {
		if m.rows[i].kind.selectable() {
			return i
		}
	}
	return from
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
	agents, count := agentRows(s)
	m.agents = count
	projects := s.Projects()
	if m.tab == ChatTab {
		m.rows, projects = append(m.rows, agents...), nil
	}
	for _, p := range projects {
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
		gap := row{kind: gapRow, project: p}
		for _, sess := range p.Sessions {
			m.rows = append(m.rows, row{kind: sessionRow, project: p, session: sess, depth: 1, sessions: []string{sess.ID}}, gap)
			if sess.ID == active {
				for _, id := range sess.TaskOrder {
					m.rows = append(m.rows, row{kind: taskRow, project: p, session: sess, task: sess.Tasks[id], depth: 2, sessions: []string{sess.ID}}, gap)
				}
			}
			for _, child := range s.Children(sess.ID) {
				m.rows = append(m.rows, row{kind: sessionRow, project: p, session: child, depth: 2, sessions: []string{child.ID}}, gap)
			}
		}
	}

	m.keys, m.models = 0, len(s.Models)
	for _, e := range s.Models {
		if e.Present {
			m.keys++
		}
	}

	if active != m.active || task != m.task || m.retarget {
		m.active, m.task, m.retarget = active, task, false
		m.cursor = m.activeRow()
	}
	m.cursor = min(max(m.cursor, 0), max(len(m.rows)-1, 0))
	if len(m.rows) > 0 && !m.rows[m.cursor].kind.selectable() {
		m.cursor = m.step(m.cursor, 1)
	}
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
	case sessionRow, agentRow:
		return r.session.ID == m.active && m.task == ""
	case taskRow, agentTaskRow:
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
	case "A", "shift+a":
		return emit(ToggleArchivedMsg{})
	case "1":
		m.SetTab(ProjectsTab)
		return nil
	case "2":
		m.SetTab(ChatTab)
		return nil
	}
	if len(m.rows) == 0 {
		return nil
	}
	if r := m.rows[m.cursor]; r.kind == sessionRow {
		switch msg.String() {
		case "d":
			return emit(DeleteMsg{SessionID: r.session.ID, Label: Title(r.session)})
		case "a":
			return emit(ArchiveMsg{SessionID: r.session.ID, Archived: !r.session.Archived})
		}
	}
	if r := m.rows[m.cursor]; r.kind == projectRow && msg.String() == "c" {
		return emit(CleanMsg{Workspace: r.project.Path, Name: r.project.Name})
	}
	switch msg.String() {
	case "up", "k":
		m.cursor = m.step(m.cursor, -1)
	case "down", "j":
		m.cursor = m.step(m.cursor, 1)
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
	case taskRow, agentTaskRow:
		return emit(SelectMsg{SessionID: r.session.ID, TaskID: r.task.ID})
	case headRow, gapRow:
		return nil
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
		first := lipgloss.Width(m.tabLabel(ProjectsTab))
		switch {
		case x < first:
			m.SetTab(ProjectsTab)
		case x < first+lipgloss.Width(m.tabLabel(ChatTab)):
			m.SetTab(ChatTab)
		case x >= m.width-lipgloss.Width(addLabel)-1 && m.tab == ProjectsTab:
			return emit(NewProjectMsg{})
		case x >= m.width-lipgloss.Width(addLabel)-1:
			return m.newChat()
		}
		return nil
	}
	i, ok := m.rowAt(y)
	if !ok || !m.rows[i].kind.selectable() {
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
	case projectRow, headRow, gapRow:
		return r.project.Name
	default:
		return Title(r.session)
	}
}

func (m Model) tabs() string {
	th := m.theme
	on := lipgloss.NewStyle().Background(th.Card).Foreground(th.Ink).Bold(true)
	var b strings.Builder
	for _, t := range []Tab{ProjectsTab, ChatTab} {
		if t == m.tab {
			b.WriteString(on.Render(m.tabLabel(t)))
		} else {
			b.WriteString(th.Muted.Render(m.tabLabel(t)))
		}
	}
	return spread(b.String(), th.Muted.Render(addLabel), max(m.width, 1))
}

func clock(d time.Duration) string {
	s := int(d / time.Second)
	if s >= 3600 {
		return fmt.Sprintf("%dh", s/3600)
	}
	return fmt.Sprintf("%d:%02d", s/60, s%60)
}

func (m Model) card(r row, cursor bool) string {
	th := m.theme
	bg := th.Card
	if m.isActive(r) {
		bg = th.Selection
	}
	on := func(st lipgloss.Style) lipgloss.Style { return st.Background(bg) }
	indent := 1
	if r.depth == 2 {
		indent = 3
	}

	var key, name, text, mark, right string
	var rightStyle lipgloss.Style
	if r.task != nil {
		key, text = r.task.Model, taskLabel(r.task)
		if key == theme.ClaudeCode || key == theme.Codex {
			name = key
		}
		right, rightStyle = taskIcon(th, r.task.Status)
		if r.task.Status == "running" {
			right, rightStyle = clock(r.task.Elapsed(time.Now())), th.Muted
		}
	} else {
		key, text = r.session.Model, Title(r.session)
		right, rightStyle = statusWord(th, r.session)
		if right == "" && r.session.Status != state.Paused {
			right, rightStyle = statusIcon(th, r.session.Status)
		}
		if r.session.SharedMemory {
			mark = sharedMark + " "
		}
	}
	textStyle := th.Text
	if cursor {
		textStyle = th.Strong
	}

	w := max(m.width-indent-1, 10)
	room := max(w-3-lipgloss.Width(mark)-lipgloss.Width(right)-2, 1)
	body := on(textStyle).Render(fit(text, room))
	if name != "" {
		name = fit(name, min(12, room))
		body = on(th.Muted).Render(name)
		if rest := room - lipgloss.Width(name) - 1; rest > 2 {
			body += on(textStyle).Render(" " + fit(text, rest))
		}
	}
	gap := max(w-3-lipgloss.Width(body)-lipgloss.Width(mark)-lipgloss.Width(right)-1, 1)
	return strings.Repeat(" ", indent) + on(th.Tint(key)).Render(" ■ ") + body + on(th.Text).Render(strings.Repeat(" ", gap)) +
		on(th.Accent).Render(mark) + on(rightStyle).Render(right) + on(th.Text).Render(" ")
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
	lines := []string{m.tabs(), ""}

	h := m.listHeight()
	for i := m.offset; i < min(m.offset+h, len(m.rows)); i++ {
		lines = append(lines, m.renderRow(m.rows[i], i == m.cursor && focused))
	}
	if m.tab == ChatTab && len(m.rows) == 0 {
		lines = append(lines, th.Faint.Render(fit(" no agents running", w)))
	}
	for len(lines) < listTop+h {
		lines = append(lines, "")
	}

	vault := spread(th.Text.Render("⚿ vault"), th.Muted.Render(fmt.Sprintf("%d of %d keys", m.keys, m.models)), w)
	legend := th.Muted.Render(fit("● run ! you ○ idle ✓ done "+sharedMark+" shared", w))
	lines = append(lines, th.Rule.Render(strings.Repeat("─", w)), vault, legend)
	view := lipgloss.NewStyle().Width(w).Height(m.height).MaxHeight(m.height).MaxWidth(w).Render(strings.Join(lines, "\n"))
	return theme.Fill(view, th.Panel, w)
}

func (m Model) renderRow(r row, cursor bool) string {
	th := m.theme
	switch r.kind {
	case gapRow:
		return ""
	case headRow:
		return th.Muted.Render(fit(" "+r.project.Name, max(m.width, 1)))
	case sessionRow, taskRow, agentRow, agentTaskRow:
		return m.card(r, cursor)
	}

	marker, icon := " ", "▾"
	if cursor {
		marker = "›"
	}
	if m.collapsed[r.project.Path] {
		icon = "▸"
	}
	right, rightStyle := fmt.Sprint(len(r.project.Sessions))+" +", th.Muted
	if n := needsYou(r.project); n > 0 {
		right, rightStyle = fmt.Sprintf("%d! +", n), th.Warn
	}
	left := 3
	text := fit(r.project.Name, max(m.width-left-lipgloss.Width(right)-1, 1))
	gap := max(m.width-left-lipgloss.Width(text)-lipgloss.Width(right), 1)
	return th.Accent.Render(marker) + th.Strong.Render(icon+" "+text) + strings.Repeat(" ", gap) + rightStyle.Render(right)
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
	case state.Interrupted:
		return "◌", th.Muted
	case state.Paused:
		return "‖", th.Muted
	default:
		return "✗", th.Warn
	}
}

func statusWord(th *theme.Theme, sess *state.Session) (string, lipgloss.Style) {
	if sess.Archived {
		return "archived", th.Muted
	}
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
	case state.Interrupted:
		return "interrupted", th.Muted
	case state.Paused, state.Working:
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
	if first, _, _ := strings.Cut(strings.TrimSpace(t.Description), "\n"); first != "" {
		return first
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

func spread(left, right string, width int) string {
	gap := max(width-lipgloss.Width(left)-lipgloss.Width(right), 1)
	return left + strings.Repeat(" ", gap) + right
}

func fit(s string, width int) string {
	return ansi.Truncate(s, width, "…")
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
