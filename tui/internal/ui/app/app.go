package app

import (
	"fmt"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"

	"fox/tui/internal/client"
	"fox/tui/internal/protocol"
	"fox/tui/internal/state"
	"fox/tui/internal/ui/approval"
	"fox/tui/internal/ui/composer"
	"fox/tui/internal/ui/layout"
	"fox/tui/internal/ui/modelpicker"
	"fox/tui/internal/ui/sidebar"
	"fox/tui/internal/ui/splash"
	"fox/tui/internal/ui/theme"
	"fox/tui/internal/ui/transcript"
	"fox/tui/internal/ui/vault"
)

type Sender interface {
	SendCmd(cmd protocol.Command) tea.Cmd
}

type Options struct {
	Version    string
	Mode       string
	Model      string
	Permission string
	Task       string
	Attach     []string
	Popout     func(args []string) error

	SidebarWidth int
	SaveSidebar  func(width int) error
}

type savedMsg struct {
	Err error
}

type sideDraft struct {
	parent string
	quote  string
}

type newChat struct {
	workspace string
	name      string
	shared    bool
	chosen    bool
}

const quoteLines = 8

type tickMsg struct{}

var spinner = []string{"◐", "◓", "◑", "◒"}

type PopoutDoneMsg struct {
	Label   string
	Command string
	Err     error
	Manual  bool
}

type focus int

const (
	focusComposer focus = iota
	focusSidebar
)

type screen int

const (
	mainScreen screen = iota
	vaultScreen
)

const wheelStep = 3

type Model struct {
	opts   Options
	send   Sender
	store  *state.Store
	theme  *theme.Theme
	frame  layout.Frame
	width  int
	height int

	active      string
	attachFocus string
	nextModel   string
	starting    int
	connected   bool
	flash       string
	focus       focus
	screen      screen
	picking     bool
	resizing    bool
	draft       *sideDraft
	activeTask  string
	working     map[string]time.Time
	ticking     bool
	confirmEnd  string
	confirmDel  string
	cleaning    string
	cleanCount  int
	sidebarW    int
	attaching   map[string]bool
	chat        newChat
	askPath     bool
	lastStart   string

	sidebar    sidebar.Model
	transcript transcript.Model
	composer   composer.Model
	picker     modelpicker.Model
	vault      vault.Model
}

func New(send Sender, store *state.Store, th *theme.Theme, opts Options) Model {
	if opts.Mode == "" {
		opts.Mode = "agent"
	}
	m := Model{
		opts:       opts,
		send:       send,
		store:      store,
		theme:      th,
		nextModel:  opts.Model,
		sidebarW:   opts.SidebarWidth,
		working:    map[string]time.Time{},
		attaching:  map[string]bool{},
		connected:  true,
		sidebar:    sidebar.New(th),
		transcript: transcript.New(th),
		composer:   composer.New(th),
		picker:     modelpicker.New(th),
		vault:      vault.New(th),
	}
	if len(opts.Attach) > 0 {
		m.attachFocus = opts.Attach[0]
	}
	m.transcript.SetBanner(splash.Banner(th, opts.Version))
	return m
}

func (m Model) Init() tea.Cmd {
	cmds := []tea.Cmd{m.send.SendCmd(protocol.ListModels{}), m.send.SendCmd(protocol.ListSessions{}),
		m.send.SendCmd(protocol.ListProjects{})}
	for _, id := range m.opts.Attach {
		cmds = append(cmds, m.send.SendCmd(protocol.AttachSession{SessionID: id}))
	}
	if task := strings.TrimSpace(m.opts.Task); task != "" {
		cmds = append(cmds, func() tea.Msg { return composer.SubmitMsg{Text: task} })
	}
	return tea.Batch(cmds...)
}

func (m Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	cmd := m.update(msg)
	m.sync()
	if len(m.working) > 0 && !m.ticking {
		m.ticking = true
		cmd = tea.Batch(cmd, tea.Tick(time.Second, func(time.Time) tea.Msg { return tickMsg{} }))
	}
	return m, cmd
}

func (m *Model) update(msg tea.Msg) tea.Cmd {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
	case client.EventMsg:
		m.onEvent(msg.Event)
	case client.DisconnectedMsg:
		m.connected = false
		m.flash = "disconnected from the server: " + msg.Err.Error()
	case client.DecodeFailedMsg:
		m.flash = "skipped an unreadable message from the server"
	case client.SendFailedMsg:
		m.flash = "couldn't send " + msg.Kind + ": " + msg.Err.Error()
		m.vault.SetBusy(false, "")
	case sidebar.SelectMsg:
		return m.selectSession(msg.SessionID, msg.TaskID)
	case sidebar.NewChatMsg:
		if msg.Workspace == "" {
			return m.startNewChat(m.currentProject())
		}
		return m.startNewChat(msg.Workspace, msg.Name)
	case sidebar.NewProjectMsg:
		return m.askProject()
	case composer.BannerMsg:
		m.toggleShared()
	case sidebar.OpenVaultMsg, modelpicker.OpenVaultMsg:
		return m.openVault()
	case sidebar.PopoutMsg:
		return m.popout(msg)
	case sidebar.DeleteMsg:
		return m.deleteChat(msg)
	case sidebar.ArchiveMsg:
		if msg.Archived {
			m.flash = "archived · shift+a shows archived chats"
		} else {
			m.flash = "back in the sidebar"
		}
		return m.send.SendCmd(protocol.ArchiveSession{SessionID: msg.SessionID, Archived: msg.Archived})
	case sidebar.CleanMsg:
		return m.cleanProject(msg)
	case sidebar.ToggleArchivedMsg:
		m.store.ShowArchived = !m.store.ShowArchived
		m.sync()
		if m.store.ShowArchived {
			m.flash = "showing archived chats · a on one brings it back · shift+a hides them"
			return m.send.SendCmd(protocol.ListSessions{IncludeArchived: true})
		}
		m.flash = "archived chats hidden"
	case PopoutDoneMsg:
		m.flash = popoutResult(msg)
	case composer.SubmitMsg:
		return m.submit(msg.Text)
	case tickMsg:
		m.ticking = false
	case composer.ToggleModeMsg:
		m.toggleMode()
	case composer.OpenPickerMsg:
		return m.openPicker()
	case modelpicker.PickMsg:
		m.nextModel, m.picking = msg.ModelKey, false
		m.flash = m.pickResult(msg.ModelKey)
	case modelpicker.CloseMsg:
		m.picking = false
	case vault.SaveMsg:
		m.vault.SetBusy(true, "saving and checking the key…")
		return m.send.SendCmd(protocol.SetModelKey{ModelKey: msg.ModelKey, Value: msg.Value})
	case vault.ValidateMsg:
		m.vault.SetBusy(true, "checking every key with its provider…")
		return m.send.SendCmd(protocol.ListModels{Validate: true})
	case vault.CloseMsg:
		m.screen = mainScreen
	case approval.DecisionMsg:
		return m.decide(msg)
	case tea.KeyPressMsg:
		return m.key(msg)
	case tea.MouseClickMsg:
		return m.click(msg)
	case tea.MouseMotionMsg:
		m.motion(msg)
	case tea.MouseReleaseMsg:
		return m.release(msg)
	case savedMsg:
		if msg.Err != nil {
			m.flash = "couldn't save the sidebar width: " + msg.Err.Error()
		}
	case tea.MouseWheelMsg:
		m.wheel(msg)
	default:
		return m.forward(msg)
	}
	return nil
}

func (m *Model) forward(msg tea.Msg) tea.Cmd {
	switch {
	case m.screen == vaultScreen:
		return m.vault.Update(msg, m.store.Models)
	case m.picking:
		return m.picker.Update(msg, m.store.Models)
	default:
		return m.composer.Update(msg)
	}
}

func (m *Model) onEvent(ev protocol.Event) {
	m.store.Apply(ev)
	switch ev.Type {
	case protocol.SessionStarted:
		if m.starting > 0 {
			m.starting--
			if sess := m.store.Session(ev.SessionID); sess != nil && sess.ParentID == "" {
				m.chat = newChat{}
			}
			m.selectSession(ev.SessionID, "")
		}
	case protocol.Attached:
		delete(m.attaching, ev.SessionID)
		if ev.SessionID == m.attachFocus {
			m.attachFocus = ""
			m.selectSession(ev.SessionID, "")
		}
	case protocol.Models:
		m.vault.SetBusy(false, "")
	case protocol.SessionRemoved:
		if m.store.Session(m.active) == nil {
			m.active, m.activeTask = "", ""
		}
	case protocol.CleanPreview:
		if p, err := protocol.Payload[protocol.RemovedPayload](ev); err == nil && m.cleaning != "" {
			m.cleanCount = p.Count
			if p.Count == 0 {
				m.flash, m.cleaning = "no stopped chats to clear here", ""
			} else {
				m.flash = fmt.Sprintf("press c again to delete %d stopped chat%s for good", p.Count, plural(p.Count))
			}
		}
	case protocol.CleanDone:
		if p, err := protocol.Payload[protocol.RemovedPayload](ev); err == nil {
			m.flash = fmt.Sprintf("deleted %d chat%s", p.Count, plural(p.Count))
		}
	case protocol.DeleteSessionDone:
		m.flash = "chat deleted"
	case protocol.Error:
		if ev.SessionID == "" && len(m.store.Notices) > 0 {
			m.flash = m.store.Notices[len(m.store.Notices)-1]
			m.vault.SetBusy(false, m.flash)
		}
		if ev.SessionID == "" && m.starting > 0 {
			m.starting--
			m.store.DropStart()
			if m.active == "" && strings.TrimSpace(m.composer.Value()) == "" {
				m.composer.SetValue(m.lastStart)
			}
		}
	}
}

func (m Model) currentProject() (string, string) {
	if m.focus == focusSidebar {
		if p, ok := m.sidebar.Project(); ok {
			return p.Path, p.Name
		}
	}
	if sess := m.store.Session(m.active); sess != nil && sess.Workspace != "" {
		return sess.Workspace, state.ProjectName(sess.Workspace)
	}
	if p, ok := m.sidebar.Project(); ok {
		return p.Path, p.Name
	}
	if m.store.DefaultWorkspace != "" {
		return "", state.ProjectName(m.store.DefaultWorkspace)
	}
	return "", "this workspace"
}

func (m Model) target() (string, string) {
	if m.chat.chosen {
		return m.chat.workspace, m.chat.name
	}
	return m.currentProject()
}

func (m *Model) startNewChat(workspace, name string) tea.Cmd {
	m.active, m.activeTask = "", ""
	m.draft = nil
	m.askPath = false
	m.chat = newChat{workspace: workspace, name: name, chosen: true}
	return m.setFocus(focusComposer)
}

func (m *Model) askProject() tea.Cmd {
	m.active, m.activeTask = "", ""
	m.draft = nil
	m.askPath = true
	return m.setFocus(focusComposer)
}

func (m *Model) chooseProject(text string) {
	path := strings.Trim(strings.TrimSpace(text), `"'`)
	m.askPath = false
	m.chat = newChat{workspace: path, name: state.ProjectName(path), chosen: true}
	m.flash = "new chat in " + m.chat.name + " · describe the task to start it"
}

func (m *Model) toggleShared() {
	if m.active != "" || m.askPath || m.draft != nil {
		m.flash = "memory is chosen when a chat starts · ctrl+n for a new chat"
		return
	}
	m.chat.workspace, m.chat.name = m.target()
	m.chat.chosen = true
	m.chat.shared = !m.chat.shared
}

func (m Model) banner() string {
	th := m.theme
	if m.active != "" || m.draft != nil {
		return ""
	}
	if m.askPath {
		return th.Accent.Render("new project") + th.Muted.Render(" · type or paste a folder path · esc cancels")
	}
	_, name := m.target()
	memory := th.Text.Render("private") + th.Muted.Render(" · ctrl+s shares it with "+name)
	if m.chat.shared {
		memory = th.Accent.Render("⇄ shared with "+name) + th.Muted.Render(" · ctrl+s makes it private")
	}
	return th.Accent.Render("new chat in "+name) + th.Muted.Render(" · memory: ") + memory
}

func (m *Model) sync() {
	if sess := m.store.Session(m.active); sess != nil {
		sess.Unread = false
	}
	for id, sess := range m.store.Sessions {
		_, seen := m.working[id]
		switch {
		case sess.Status == state.Working && !seen:
			m.working[id] = time.Now()
		case sess.Status != state.Working && seen:
			delete(m.working, id)
		}
	}
	m.sidebar.Refresh(m.store, m.active, m.activeTask)
	m.composer.SetModel(m.modelLabel())
	m.composer.SetMode(m.modeLabel())
	m.composer.SetPlaceholder(m.placeholder())
	m.composer.SetBanner(m.banner())
	if m.width == 0 {
		return
	}
	m.frame = layout.Compute(m.width, m.height, m.sidebarW, m.composer.Height())
	m.composer.SetWidth(m.frame.Main.W - 2)
	m.frame = layout.Compute(m.width, m.height, m.sidebarW, m.composer.Height())
	m.sidebar.SetSize(m.frame.Sidebar.W, m.frame.Sidebar.H)
	status := 0
	if m.statusLine() != "" {
		status = 1
	}
	m.transcript.SetSize(max(m.frame.Body.W-2, 1), max(m.frame.Body.H-status, 1))
	m.transcript.Sync(m.store.Session(m.active), m.activeTask)
}

func (m *Model) selectSession(id, task string) tea.Cmd {
	m.active, m.activeTask = id, task
	m.draft = nil
	m.picking = false
	focus := m.setFocus(focusComposer)
	sess := m.store.Session(id)
	if sess == nil || sess.Loaded || m.attaching[id] {
		return focus
	}
	m.attaching[id] = true
	return tea.Batch(focus, m.send.SendCmd(protocol.AttachSession{SessionID: id}))
}

func (m *Model) setFocus(f focus) tea.Cmd {
	if !m.frame.HasSidebar && m.width > 0 {
		f = focusComposer
	}
	m.focus = f
	if f == focusSidebar {
		m.composer.Blur()
		return nil
	}
	return m.composer.Focus()
}

func (m *Model) submit(text string) tea.Cmd {
	if !m.connected {
		m.composer.SetValue(text)
		m.flash = "not connected to the server"
		return nil
	}
	if m.draft != nil {
		return m.startSide(text)
	}
	if m.active == "" && m.askPath {
		m.chooseProject(text)
		return nil
	}
	if m.active == "" {
		workspace, _ := m.target()
		m.store.ExpectStart(state.PendingStart{Task: text, Mode: m.opts.Mode})
		m.starting++
		m.lastStart = text
		return m.send.SendCmd(protocol.StartSession{
			Task:           text,
			ModelKey:       m.nextModel,
			Mode:           m.opts.Mode,
			PermissionMode: m.opts.Permission,
			Workspace:      workspace,
			SharedMemory:   m.chat.shared,
		})
	}
	sess := m.store.Session(m.active)
	if sess != nil && !sess.Loaded {
		m.composer.SetValue(text)
		m.flash = "still loading this session · try again in a moment"
		return nil
	}
	if sess == nil || !resumable(sess) {
		m.composer.SetValue(text)
		m.flash = "this session has ended · ctrl+n starts a new one"
		return nil
	}
	if m.activeTask != "" {
		return m.sendToTask(sess, text)
	}
	m.store.AddUserMessage(m.active, "", text)
	return m.send.SendCmd(protocol.HumanMessage{SessionID: m.active, Content: text})
}

func resumable(sess *state.Session) bool {
	switch {
	case sess.Status == state.Failed:
		return false
	case sess.Status == state.Done || sess.Status.Stopped():
		return sess.ParentID == ""
	}
	return true
}

func (m *Model) sendToTask(sess *state.Session, text string) tea.Cmd {
	task := sess.Tasks[m.activeTask]
	if task == nil {
		m.composer.SetValue(text)
		m.flash = "this executor is gone · esc goes back to the orchestrator"
		return nil
	}
	id := task.ID
	m.store.AddUserMessage(sess.ID, id, text)
	return m.send.SendCmd(protocol.HumanMessage{SessionID: sess.ID, TaskID: &id, Content: text})
}

func taskFinished(status string) bool {
	switch status {
	case "done", "failed", "cancelled", "escalated":
		return true
	}
	return false
}

func (m *Model) openSide() tea.Cmd {
	sess := m.store.Session(m.active)
	if sess == nil {
		m.flash = "start a session first, then ctrl+t opens a side chat about it"
		return nil
	}
	m.draft = &sideDraft{parent: sess.ID, quote: m.transcript.Selection()}
	m.transcript.ClearSelection()
	return m.setFocus(focusComposer)
}

func (m *Model) startSide(text string) tea.Cmd {
	d := m.draft
	m.draft = nil
	parent := m.store.Session(d.parent)
	if parent == nil {
		m.flash = "that session is gone"
		return nil
	}
	task := text
	if d.quote != "" {
		task += "\n\nThis is about this part of the conversation:\n" + quote(d.quote)
	}
	m.store.ExpectStart(state.PendingStart{Task: task, Mode: "agent", ParentID: parent.ID})
	m.starting++
	model := m.nextModel
	if model == "" {
		model = parent.Model
	}
	return m.send.SendCmd(protocol.StartSession{Task: task, ModelKey: model, Mode: "agent", ParentSessionID: parent.ID})
}

func quote(s string) string {
	lines := strings.Split(s, "\n")
	for i, l := range lines {
		lines[i] = "> " + l
	}
	return strings.Join(lines, "\n")
}

func (m *Model) decide(d approval.DecisionMsg) tea.Cmd {
	a, ok := m.store.ResolveApproval(d.Approval.SessionID, d.Approval.RequestID)
	if !ok {
		return nil
	}
	return m.send.SendCmd(protocol.ApprovalResponse{
		SessionID: a.SessionID,
		RequestID: a.RequestID,
		ToolName:  a.ToolName,
		Approved:  d.Approved,
		Always:    d.Always,
	})
}

func (m *Model) openVault() tea.Cmd {
	m.screen, m.picking = vaultScreen, false
	m.vault.Open()
	m.vault.SetBusy(true, "")
	return m.send.SendCmd(protocol.ListModels{})
}

func (m *Model) openPicker() tea.Cmd {
	m.picking = true
	note := "applies to this new session"
	if m.active != "" {
		note = "applies to new sessions and side threads"
	}
	cmds := []tea.Cmd{m.picker.Open(m.modelLabel(), note)}
	if len(m.store.Models) == 0 {
		cmds = append(cmds, m.send.SendCmd(protocol.ListModels{}))
	}
	return tea.Batch(cmds...)
}

func (m *Model) popout(msg sidebar.PopoutMsg) tea.Cmd {
	var args []string
	for _, id := range msg.SessionIDs {
		args = append(args, "--attach", id)
	}
	open, command := m.opts.Popout, "fox "+strings.Join(args, " ")
	return func() tea.Msg {
		if open == nil {
			return PopoutDoneMsg{Label: msg.Label, Command: command, Manual: true}
		}
		return PopoutDoneMsg{Label: msg.Label, Command: command, Err: open(args)}
	}
}

func popoutResult(msg PopoutDoneMsg) string {
	switch {
	case msg.Manual:
		return "run this in another terminal: " + msg.Command
	case msg.Err != nil:
		return "couldn't open a window (" + msg.Err.Error() + "), run: " + msg.Command
	default:
		return "opened " + msg.Label + " in a new window"
	}
}

func (m *Model) pendingApproval() (state.Approval, int, bool) {
	var first state.Approval
	n := 0
	for _, a := range m.store.Approvals {
		if a.SessionID != m.active || a.Handoff() {
			continue
		}
		if n == 0 {
			first = a
		}
		n++
	}
	return first, n - 1, n > 0
}

func (m *Model) key(msg tea.KeyPressMsg) tea.Cmd {
	k := msg.String()
	if k == "ctrl+c" {
		return tea.Quit
	}
	m.flash = ""
	if k != "ctrl+x" {
		m.confirmEnd = ""
	}
	if k != "d" {
		m.confirmDel = ""
	}
	if k != "c" {
		m.cleaning, m.cleanCount = "", 0
	}
	if m.screen == vaultScreen {
		return m.vault.Update(msg, m.store.Models)
	}
	if a, _, ok := m.pendingApproval(); ok {
		return approval.Update(msg, a)
	}
	if m.picking {
		return m.picker.Update(msg, m.store.Models)
	}

	switch k {
	case "tab":
		if m.focus == focusComposer {
			return m.setFocus(focusSidebar)
		}
		return m.setFocus(focusComposer)
	case "esc":
		switch {
		case m.focus == focusSidebar:
			return m.setFocus(focusComposer)
		case m.draft != nil:
			m.draft = nil
		case m.askPath:
			m.askPath = false
		case m.transcript.HasSelection():
			m.transcript.ClearSelection()
		case m.activeTask != "":
			m.activeTask = ""
		}
		return nil
	case "ctrl+n":
		return m.startNewChat(m.currentProject())
	case "ctrl+s":
		m.toggleShared()
		return nil
	case "ctrl+p":
		return m.openPicker()
	case "ctrl+k":
		return m.openVault()
	case "ctrl+t":
		return m.openSide()
	case "ctrl+o":
		return m.popoutCurrent()
	case "shift+tab":
		m.toggleMode()
		return nil
	case "ctrl+r":
		m.transcript.ToggleAll()
		return nil
	case "ctrl+x":
		return m.endSession()
	case "ctrl+g":
		if h, ok := m.pendingHandoff(); ok {
			return m.decide(approval.DecisionMsg{Approval: h})
		}
	case "pgup":
		m.transcript.PageUp()
		return nil
	case "pgdown":
		m.transcript.PageDown()
		return nil
	case "shift+up":
		m.transcript.ScrollUp(1)
		return nil
	case "shift+down":
		m.transcript.ScrollDown(1)
		return nil
	}

	if m.focus == focusSidebar {
		return m.sidebar.Key(msg)
	}
	return m.composer.Update(msg)
}

func (m *Model) deleteChat(msg sidebar.DeleteMsg) tea.Cmd {
	if m.confirmDel != msg.SessionID {
		m.confirmDel = msg.SessionID
		m.flash = fmt.Sprintf("press d again to delete %q for good", msg.Label)
		return nil
	}
	m.confirmDel = ""
	m.flash = "deleting…"
	return m.send.SendCmd(protocol.DeleteSession{SessionID: msg.SessionID})
}

func (m *Model) cleanProject(msg sidebar.CleanMsg) tea.Cmd {
	if m.cleaning == msg.Workspace && m.cleanCount > 0 {
		m.cleaning, m.cleanCount = "", 0
		m.flash = "clearing…"
		return m.send.SendCmd(protocol.CleanSessions{Project: msg.Workspace})
	}
	m.cleaning, m.cleanCount = msg.Workspace, 0
	m.flash = "counting stopped chats in " + msg.Name + "…"
	return m.send.SendCmd(protocol.CleanSessions{Project: msg.Workspace, DryRun: true})
}

func (m *Model) popoutCurrent() tea.Cmd {
	if m.focus == focusSidebar {
		if msg, ok := m.sidebar.Selected(); ok {
			return m.popout(msg)
		}
		return nil
	}
	sess := m.store.Session(m.active)
	if sess == nil {
		m.flash = "start a session first, then ctrl+o opens it in a new window"
		return nil
	}
	return m.popout(sidebar.PopoutMsg{Label: sidebar.Title(sess), SessionIDs: []string{sess.ID}})
}

func (m *Model) click(msg tea.MouseClickMsg) tea.Cmd {
	f := m.frame
	if m.screen == vaultScreen {
		return m.vault.Press(msg.X, msg.Y, m.store.Models)
	}
	if _, _, ok := m.pendingApproval(); ok {
		return nil
	}
	if m.picking {
		box := m.picker.View(m.store.Models)
		r := overlayRect(box, f.Main)
		if !r.Contains(msg.X, msg.Y) {
			m.picking = false
			return nil
		}
		_, y := r.Local(msg.X, msg.Y)
		return m.picker.Press(y, m.store.Models)
	}
	switch {
	case f.HasSidebar && f.Divider.Contains(msg.X, msg.Y):
		m.resizing = true
	case f.HasSidebar && f.Sidebar.Contains(msg.X, msg.Y):
		x, y := f.Sidebar.Local(msg.X, msg.Y)
		return m.sidebar.Press(x, y, msg.Button)
	case m.active != "" && m.draft == nil && f.Body.Contains(msg.X, msg.Y):
		x, y := f.Body.Local(msg.X, msg.Y)
		m.transcript.Press(x-1, y)
	case f.Composer.Contains(msg.X, msg.Y):
		x, y := f.Composer.Local(msg.X, msg.Y)
		m.focus = focusComposer
		return m.composer.Press(x-1, y)
	}
	return nil
}

func (m *Model) motion(msg tea.MouseMotionMsg) {
	if m.resizing {
		m.sidebarW = layout.SidebarWidth(m.width, msg.X)
		return
	}
	if m.transcript.Dragging() {
		m.transcript.Drag(msg.X-m.frame.Body.X-1, msg.Y-m.frame.Body.Y)
		return
	}
	m.sidebar.Motion(msg.X, msg.Y)
}

func (m *Model) release(msg tea.MouseReleaseMsg) tea.Cmd {
	if m.resizing {
		m.resizing = false
		save, width := m.opts.SaveSidebar, m.sidebarW
		if save == nil {
			return nil
		}
		return func() tea.Msg { return savedMsg{Err: save(width)} }
	}
	if m.transcript.Dragging() {
		m.transcript.Release()
		return nil
	}
	return m.sidebar.Release(m.frame.DropZone.Contains(msg.X, msg.Y))
}

func (m *Model) wheel(msg tea.MouseWheelMsg) {
	delta := wheelStep
	if msg.Button == tea.MouseWheelUp {
		delta = -wheelStep
	}
	if m.frame.HasSidebar && m.frame.Sidebar.Contains(msg.X, msg.Y) {
		m.sidebar.Scroll(delta)
		return
	}
	if delta < 0 {
		m.transcript.ScrollUp(-delta)
	} else {
		m.transcript.ScrollDown(delta)
	}
}

func (m Model) pickResult(model string) string {
	sess := m.store.Session(m.active)
	if sess == nil || sess.Model == "" || sess.Model == model {
		return "new sessions will use " + model
	}
	return "this session stays on " + sess.Model + " · ctrl+n starts one on " + model
}

func (m Model) modelLabel() string {
	if t := m.task(); t != nil && t.Model != "" {
		return t.Model
	}
	if sess := m.store.Session(m.active); sess != nil && sess.Model != "" {
		return sess.Model
	}
	return m.nextModel
}

func (m Model) placeholder() string {
	sess := m.store.Session(m.active)
	switch {
	case !m.connected:
		return "disconnected"
	case m.draft != nil:
		return "ask a side question…"
	case m.activeTask != "" && sess != nil && sess.Tasks[m.activeTask] != nil && taskFinished(sess.Tasks[m.activeTask].Status):
		return "this executor has finished · a message starts it again with everything it knew"
	case m.activeTask != "":
		return "message this executor…"
	case m.active == "" && m.askPath:
		return "folder path, for example C:\\Users\\you\\projects\\api"
	case m.active == "":
		return "describe a task to start a chat…"
	case sess != nil && sess.Status.Stopped():
		return "send a message to pick this chat up again…"
	case sess != nil && !resumable(sess):
		return "this session has ended · ctrl+n for a new one"
	default:
		return "ask fox…"
	}
}

func (m Model) hint() string {
	if m.flash != "" {
		return m.flash
	}
	if m.focus == focusSidebar {
		return "↑↓ move · enter open · n new chat · d delete · a archive · c clear project · ←→ fold · tab back"
	}
	switch {
	case m.draft != nil:
		return "enter starts the side chat · esc cancels"
	case m.active == "" && m.askPath:
		return "enter picks the folder · esc cancels"
	case m.active == "":
		return "enter starts the chat · ctrl+s shared memory · tab sidebar"
	case m.activeTask != "":
		return "enter messages this executor · esc back to the orchestrator"
	case m.transcript.HasSelection():
		return "ctrl+t asks about the selection · esc clears"
	}
	return "enter send · alt+enter newline · ctrl+t side chat · tab sidebar"
}

func (m Model) View() tea.View {
	v := tea.NewView(m.render())
	v.AltScreen = true
	v.MouseMode = tea.MouseModeCellMotion
	v.BackgroundColor = m.theme.Background
	v.ForegroundColor = m.theme.Ink
	v.WindowTitle = "fox"
	return v
}

func (m Model) render() string {
	th := m.theme
	if m.width == 0 {
		return th.Muted.Render("starting…")
	}
	if m.screen == vaultScreen {
		return m.vault.View(m.store.Models, m.width, m.height)
	}
	f := m.frame
	base := m.mainView()
	if f.HasSidebar {
		rule := th.Rule
		if m.resizing {
			rule = th.Accent
		}
		divider := rule.Render(strings.TrimSuffix(strings.Repeat("│\n", f.Divider.H), "\n"))
		base = lipgloss.JoinHorizontal(lipgloss.Top, m.sidebar.View(m.focus == focusSidebar), divider, base)
	}
	base = lipgloss.NewStyle().Width(m.width).Height(m.height).MaxWidth(m.width).MaxHeight(m.height).Render(base)

	layers := []*lipgloss.Layer{lipgloss.NewLayer(base)}
	if m.picking {
		box := m.picker.View(m.store.Models)
		r := overlayRect(box, f.Main)
		layers = append(layers, lipgloss.NewLayer(box).X(r.X).Y(r.Y).Z(1))
	}
	if a, waiting, ok := m.pendingApproval(); ok {
		box := approval.View(th, a, m.where(a), waiting, f.Main.W)
		r := overlayRect(box, f.Main)
		layers = append(layers, lipgloss.NewLayer(box).X(r.X).Y(r.Y).Z(2))
	}
	if label, x, y := m.sidebar.Ghost(); label != "" {
		layers = append(layers,
			lipgloss.NewLayer(m.dropZone()).X(f.DropZone.X).Y(f.DropZone.Y).Z(3),
			lipgloss.NewLayer(th.Accent.Background(th.Selection).Render(" ⧉ "+label+" ")).X(min(x+1, m.width-1)).Y(y).Z(4),
		)
	}
	if len(layers) == 1 {
		return base
	}
	return lipgloss.NewCompositor(layers...).Render()
}

func (m Model) mainView() string {
	th := m.theme
	f := m.frame
	var body string
	if m.active == "" {
		body = lipgloss.NewStyle().MaxWidth(f.Body.W).MaxHeight(f.Body.H).Render(splash.View(th, f.Body.W, f.Body.H, splash.Info{
			Version:    m.opts.Version,
			Model:      m.modelLabel(),
			Mode:       m.opts.Mode,
			Permission: m.opts.Permission,
		}))
	} else if m.draft != nil {
		body = lipgloss.NewStyle().Width(f.Body.W).Height(f.Body.H).MaxHeight(f.Body.H).PaddingLeft(1).
			Render(m.draftView(f.Body.W - 2))
	} else {
		view := m.transcript.View()
		if line := m.statusLine(); line != "" {
			view += "\n" + line
		}
		body = lipgloss.NewStyle().Width(f.Body.W).Height(f.Body.H).MaxHeight(f.Body.H).PaddingLeft(1).
			Render(view)
	}
	return lipgloss.JoinVertical(lipgloss.Left,
		m.header(f.Header.W),
		th.Rule.Render(strings.Repeat("─", f.Rule.W)),
		body,
		lipgloss.NewStyle().PaddingLeft(1).Render(m.composer.View(m.hint())),
	)
}

func (m Model) header(width int) string {
	th := m.theme
	sess := m.store.Session(m.active)
	var left string
	if t := m.task(); t != nil {
		left = th.Strong.Render("exec "+t.ID) + th.Muted.Render(" · from "+sidebar.Title(sess))
	} else if m.draft != nil && sess != nil {
		left = th.Strong.Render("side chat") + th.Muted.Render(" · from "+sidebar.Title(sess))
	} else if sess == nil && m.askPath {
		left = th.Strong.Render("new project")
	} else if sess == nil {
		_, name := m.target()
		left = th.Strong.Render("new chat") + th.Muted.Render(" · in "+name+" · "+m.opts.Mode)
	} else {
		left = th.Strong.Render(sidebar.Title(sess)) + th.Muted.Render(details(sess.Mode, sess.PermissionMode, sess.Workspace))
	}
	right := ""
	if sess != nil {
		right = th.Muted.Render(usage(sess))
	}
	if !m.connected {
		right = th.Warn.Render("disconnected")
	}
	if lipgloss.Width(left)+lipgloss.Width(right)+3 > width && sess != nil {
		left = th.Strong.Render(sidebar.Title(sess))
	}
	gap := max(width-2-lipgloss.Width(left)-lipgloss.Width(right), 1)
	line := " " + left + strings.Repeat(" ", gap) + right + " "
	return lipgloss.NewStyle().MaxWidth(width).Render(line)
}

func (m *Model) toggleMode() {
	next := "orchestrator"
	if m.opts.Mode == "orchestrator" {
		next = "agent"
	}
	m.opts.Mode = next
	if sess := m.store.Session(m.active); sess != nil && sess.Mode != "" && sess.Mode != next {
		m.flash = "this session stays " + sess.Mode + " · ctrl+n starts a new " + next + " session"
		return
	}
	m.flash = "new sessions start in " + next + " mode"
}

func (m Model) modeLabel() string {
	if sess := m.store.Session(m.active); sess != nil && sess.Mode != "" {
		return sess.Mode
	}
	return m.opts.Mode
}

func (m *Model) endSession() tea.Cmd {
	sess := m.store.Session(m.active)
	if sess == nil || sess.Status == state.Done || sess.Status == state.Failed || sess.Status.Stopped() {
		m.flash = "no running session to end"
		return nil
	}
	if m.confirmEnd != sess.ID {
		m.confirmEnd = sess.ID
		m.flash = "press ctrl+x again to end this session"
		return nil
	}
	m.confirmEnd = ""
	m.flash = "ending the session…"
	return m.send.SendCmd(protocol.EndSession{SessionID: sess.ID})
}

func (m Model) pendingHandoff() (state.Approval, bool) {
	for _, a := range m.store.Approvals {
		if a.SessionID == m.active && a.Handoff() {
			return a, true
		}
	}
	return state.Approval{}, false
}

func (m Model) handoffLine(h state.Approval) string {
	th := m.theme
	who := ""
	if h.TaskID != "" {
		who = " (exec " + h.TaskID + ")"
	}
	return th.Warn.Bold(true).Render("◆ waiting for you in the browser"+who+":") + " " +
		th.Text.Render(h.Description) + th.Muted.Render("  ·  ctrl+g skip")
}

func (m Model) statusLine() string {
	sess := m.store.Session(m.active)
	if sess == nil || m.draft != nil {
		return ""
	}
	if h, ok := m.pendingHandoff(); ok {
		return m.handoffLine(h)
	}
	if m.activeTask != "" {
		return ""
	}
	since, ok := m.working[sess.ID]
	if !ok {
		return ""
	}
	elapsed := time.Since(since)
	text := "working · " + clock(elapsed)
	if n := runningTasks(sess); n > 0 {
		text += fmt.Sprintf(" · %d executor%s running", n, plural(n))
	}
	frame := spinner[int(elapsed/time.Second)%len(spinner)]
	return m.theme.Accent.Render(frame) + " " + m.theme.Muted.Render(text)
}

func runningTasks(sess *state.Session) int {
	n := 0
	for _, t := range sess.Tasks {
		if t.Status == "running" || t.Status == "needs-approval" {
			n++
		}
	}
	return n
}

func clock(d time.Duration) string {
	s := int(d / time.Second)
	if s < 60 {
		return fmt.Sprintf("%ds", s)
	}
	return fmt.Sprintf("%dm%02ds", s/60, s%60)
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

func (m Model) task() *state.Task {
	sess := m.store.Session(m.active)
	if sess == nil || m.activeTask == "" {
		return nil
	}
	return sess.Tasks[m.activeTask]
}

func (m Model) draftView(width int) string {
	th := m.theme
	lines := []string{
		"",
		th.Accent.Bold(true).Render("side chat"),
		th.Muted.Width(width).Render("A separate agent that knows the main task. The main session keeps running; nothing here changes it."),
	}
	if q := m.draft.quote; q != "" {
		quoted := strings.Split(q, "\n")
		if len(quoted) > quoteLines {
			quoted = append(quoted[:quoteLines], "…")
		}
		lines = append(lines, "", th.Heading.Render("about"))
		for _, l := range quoted {
			lines = append(lines, th.Accent.Render("│ ")+th.Text.Render(ansi.Truncate(l, max(width-2, 1), "…")))
		}
	} else {
		lines = append(lines, "", th.Muted.Width(width).Render("Tip: drag over text in the conversation first, then ctrl+t, to ask about just that part."))
	}
	return strings.Join(lines, "\n")
}

func (m Model) where(a state.Approval) string {
	s := sidebar.Title(m.store.Session(a.SessionID))
	if a.TaskID != "" {
		s += " · exec " + a.TaskID
	}
	return s
}

func (m Model) dropZone() string {
	th := m.theme
	r := m.frame.DropZone
	return lipgloss.NewStyle().
		Width(r.W).
		Height(r.H).
		Background(th.Selection).
		Border(lipgloss.Border{Left: "┆"}, false, false, false, true).
		BorderForeground(th.Primary).
		BorderBackground(th.Selection).
		Align(lipgloss.Center, lipgloss.Center).
		Render(th.Accent.Bold(true).Background(th.Selection).Render("⧉ open in new window") + "\n" +
			th.Muted.Background(th.Selection).Render("release to drop"))
}

func details(parts ...string) string {
	var out []string
	for _, p := range parts {
		if p != "" {
			out = append(out, p)
		}
	}
	if len(out) == 0 {
		return ""
	}
	return " · " + strings.Join(out, " · ")
}

func usage(sess *state.Session) string {
	tokens := sess.InputTokens + sess.OutputTokens
	if tokens == 0 && sess.Cost == 0 {
		return ""
	}
	cost := fmt.Sprintf("$%.3f", sess.Cost)
	if sess.CostPartial {
		cost = "≥" + cost
	}
	return cost + " · " + compact(tokens) + " tok"
}

func compact(n int) string {
	switch {
	case n >= 1_000_000:
		return fmt.Sprintf("%.1fM", float64(n)/1_000_000)
	case n >= 1_000:
		return fmt.Sprintf("%.1fk", float64(n)/1_000)
	default:
		return fmt.Sprint(n)
	}
}

func overlayRect(box string, area layout.Rect) layout.Rect {
	w, h := lipgloss.Width(box), lipgloss.Height(box)
	return layout.Rect{
		X: area.X + max((area.W-w)/2, 0),
		Y: area.Y + max((area.H-h)/2, 0),
		W: w,
		H: h,
	}
}
