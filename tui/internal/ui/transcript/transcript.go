package transcript

import (
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"

	"charm.land/bubbles/v2/viewport"
	"charm.land/lipgloss/v2"

	"fox/tui/internal/protocol"
	"fox/tui/internal/state"
	"fox/tui/internal/ui/theme"
)

const (
	minWidth     = 12
	cardActivity = 2
)

type Model struct {
	theme     *theme.Theme
	view      viewport.Model
	key       string
	lines     []string
	sel       selection
	banner    string
	open      map[string]bool
	expandAll bool
	dirty     bool
	toggles   map[int]string
}

func New(th *theme.Theme) Model {
	vp := viewport.New()
	vp.SoftWrap = true
	return Model{theme: th, view: vp, open: map[string]bool{}}
}

func (m *Model) SetSize(w, h int) {
	if w == m.view.Width() && h == m.view.Height() {
		return
	}
	if w != m.view.Width() {
		m.ClearSelection()
	}
	m.view.SetWidth(w)
	m.view.SetHeight(h)
	m.key = ""
}

func (m *Model) Sync(sess *state.Session, task string) {
	key := task + "|" + cacheKey(sess, m.view.Width())
	if key == m.key && !m.dirty {
		return
	}
	m.dirty = false
	switched := !strings.HasPrefix(key, task+"|"+sessionPrefix(sess))
	following := m.view.AtBottom() || m.key == "" || switched
	if switched {
		m.ClearSelection()
	}
	m.key = key
	text, toggles := join(m.render(sess, task))
	m.toggles = toggles
	m.lines = strings.Split(text, "\n")
	m.paint()
	if following {
		m.view.GotoBottom()
	}
}

func (m *Model) SetBanner(s string) {
	m.banner = s
	m.key = ""
}

func (m *Model) ScrollUp(n int)   { m.view.ScrollUp(n) }
func (m *Model) ScrollDown(n int) { m.view.ScrollDown(n) }
func (m *Model) PageUp()          { m.view.PageUp() }
func (m *Model) PageDown()        { m.view.PageDown() }

func (m Model) View() string {
	return m.view.View()
}

func sessionPrefix(sess *state.Session) string {
	if sess == nil {
		return "|"
	}
	return sess.ID + "|"
}

func cacheKey(sess *state.Session, width int) string {
	if sess == nil {
		return fmt.Sprintf("|%d", width)
	}
	pending := 0
	for _, e := range sess.Entries {
		if e.Pending {
			pending++
		}
	}
	var tasks strings.Builder
	for _, id := range sess.TaskOrder {
		tasks.WriteString(sess.Tasks[id].Status)
	}
	return fmt.Sprintf("%s%d|%d|%d|%s|%d|%t", sessionPrefix(sess), width, len(sess.Entries), pending, tasks.String(), sess.Status, sess.Loaded)
}

func (m Model) render(sess *state.Session, task string) []block {
	th := m.theme
	width := max(m.view.Width(), minWidth)
	if sess == nil {
		return []block{{text: th.Faint.Render("waiting for the agent…")}}
	}
	if task != "" {
		return m.renderTask(sess, task, width)
	}
	if !sess.Loaded {
		return []block{{text: th.Faint.Render("loading this session…")}}
	}
	blocks := m.blocks(sess, "", width)
	if len(blocks) == 0 {
		blocks = []block{{text: th.Faint.Render("waiting for the agent…")}}
	}
	if m.banner != "" {
		blocks = append([]block{{text: "\n" + m.banner}}, blocks...)
	}
	return blocks
}

func (m Model) renderTask(sess *state.Session, taskID string, width int) []block {
	th := m.theme
	t := sess.Tasks[taskID]
	if t == nil {
		t = &state.Task{ID: taskID}
	}
	head := th.Muted.Bold(true).Render("orchestrator → exec " + t.ID)
	if t.Lane != "" {
		head += th.Muted.Render(" · " + t.Lane + " lane")
	}
	blocks := []block{{text: head + "\n" + markdown(th, t.Description, width)}}
	blocks = append(blocks, m.blocks(sess, taskID, width)...)
	icon, live := taskIcon(t.Status)
	switch {
	case t.Status == "failed" && t.Error != "":
		blocks = append(blocks, block{text: th.Warn.Width(width).Render(icon + " failed: " + t.Error)})
	case t.Report != nil:
		blocks = append(blocks, block{text: th.Muted.Width(width).Render(icon + " reported: " + reportLine(t.Report))})
	case live:
		blocks = append(blocks, block{text: th.Accent.Render(icon + " working…")})
	case t.Status != "":
		blocks = append(blocks, block{text: th.Muted.Render(icon + " " + t.Status)})
	}
	return blocks
}

func (m Model) blocks(sess *state.Session, task string, width int) []block {
	var blocks []block
	carded := map[string]bool{}
	for i := 0; i < len(sess.Entries); i++ {
		e := sess.Entries[i]
		if e.TaskID != task {
			if task == "" && !carded[e.TaskID] {
				carded[e.TaskID] = true
				blocks = append(blocks, block{text: m.card(sess, e.TaskID, width)})
			}
			continue
		}
		if isTrace(e.Kind) {
			calls, last := collect(sess.Entries, i, task)
			blocks = append(blocks, m.traceBlock(calls, fmt.Sprintf("%s|%s|%d", sess.ID, task, i), width))
			i = last
			continue
		}
		blocks = append(blocks, block{text: m.entry(e, width)})
	}
	return blocks
}

func nextResult(entries []state.Entry, call int) (int, bool) {
	for j := call + 1; j < len(entries); j++ {
		e := entries[j]
		if e.TaskID != entries[call].TaskID {
			continue
		}
		if (e.Kind == state.ToolResultEntry || e.Kind == state.DeniedEntry) && e.Tool == entries[call].Tool {
			return j, true
		}
		return 0, false
	}
	return 0, false
}

func outcome(e state.Entry) string {
	if e.Kind == state.DeniedEntry {
		return "denied: " + oneLine(e.Text)
	}
	return summarize(e.Text)
}

func (m Model) entry(e state.Entry, width int) string {
	th := m.theme
	switch e.Kind {
	case state.UserEntry:
		block := lipgloss.NewStyle().Background(th.UserBg).Foreground(th.Ink).Padding(0, 1).Width(width).Render(e.Text)
		if e.Pending {
			block += "\n" + th.Faint.Render("sending…")
		}
		return block
	case state.AgentEntry:
		return markdown(th, e.Text, width)
	case state.ToolResultEntry:
		return th.Faint.Width(width).Render("  " + summarize(e.Text))
	case state.DeniedEntry:
		return th.Warn.Width(width).Render("✗ denied " + e.Tool + ": " + oneLine(e.Text))
	case state.ErrorEntry:
		return th.Warn.Width(width).Render(e.Text)
	default:
		return th.Muted.Italic(true).Width(width).Render(e.Text)
	}
}

func (m Model) card(sess *state.Session, taskID string, width int) string {
	th := m.theme
	task := sess.Tasks[taskID]
	if task == nil {
		task = &state.Task{ID: taskID}
	}

	icon, live := taskIcon(task.Status)
	name := "exec · " + label(task)
	if task.Lane != "" {
		name += th.Muted.Render(" · " + task.Lane + " lane")
	}
	meta := task.Model
	if task.Cost > 0 {
		meta += fmt.Sprintf(" · $%.3f", task.Cost)
	}
	iconStyle := th.Muted
	if live {
		iconStyle = th.Accent
	}
	head := iconStyle.Render(icon) + " " + th.Text.Render(name)
	gap := max(width-4-lipgloss.Width(head)-lipgloss.Width(meta), 1)
	lines := []string{head + strings.Repeat(" ", gap) + th.Muted.Render(meta)}

	switch {
	case task.Status == "failed" && task.Error != "":
		lines = append(lines, th.Warn.Render(oneLine(task.Error)))
	case task.Report != nil:
		lines = append(lines, th.Muted.Render(reportLine(task.Report)))
	default:
		for _, a := range activity(sess.Entries, taskID, cardActivity) {
			lines = append(lines, th.Muted.Render(a))
		}
	}

	border := th.Edge
	if live {
		border = th.Dim
	}
	return lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(border).
		Padding(0, 1).
		Width(width).
		Render(strings.Join(lines, "\n"))
}

func activity(entries []state.Entry, taskID string, n int) []string {
	var out []string
	for i := 0; i < len(entries); i++ {
		e := entries[i]
		if e.TaskID != taskID {
			continue
		}
		switch e.Kind {
		case state.ToolCallEntry:
			line := "→ " + e.Tool + "(" + formatArgs(e.Args) + ")"
			if j, ok := nextResult(entries, i); ok {
				line += " · " + outcome(entries[j])
			}
			out = append(out, line)
		case state.AgentEntry, state.NoticeEntry, state.ErrorEntry:
			out = append(out, truncate(oneLine(e.Text), 120))
		}
	}
	if len(out) > n {
		out = out[len(out)-n:]
	}
	return out
}

func reportLine(r *protocol.Report) string {
	parts := []string{}
	if r.Outcome != "" {
		parts = append(parts, r.Outcome)
	}
	if len(r.Changes) > 0 {
		parts = append(parts, fmt.Sprintf("%d files", len(r.Changes)))
	}
	if len(r.Blockers) > 0 {
		parts = append(parts, fmt.Sprintf("%d blockers", len(r.Blockers)))
	}
	parts = append(parts, "report delivered")
	return strings.Join(parts, " · ")
}

func taskIcon(status string) (string, bool) {
	switch status {
	case "running":
		return "◐", true
	case "needs-approval":
		return "!", true
	case "done":
		return "✓", false
	case "failed":
		return "✗", false
	case "cancelled":
		return "–", false
	case "escalated":
		return "↑", false
	default:
		return "·", false
	}
}

func label(t *state.Task) string {
	if len(t.Scope) > 0 {
		return strings.Join(t.Scope, ", ")
	}
	if t.Description != "" {
		return truncate(oneLine(t.Description), 60)
	}
	return t.ID
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}

func oneLine(s string) string {
	return strings.Join(strings.Fields(s), " ")
}

func numText(v any) string {
	if f, ok := v.(float64); ok {
		return strconv.FormatFloat(f, 'f', -1, 64)
	}
	return fmt.Sprint(v)
}

func formatArgs(args map[string]any) string {
	if len(args) == 0 {
		return ""
	}
	keys := make([]string, 0, len(args))
	for k := range args {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	if len(keys) == 1 {
		return truncate(oneLine(fmt.Sprint(args[keys[0]])), 70)
	}
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+truncate(oneLine(fmt.Sprint(args[k])), 40))
	}
	return strings.Join(parts, ", ")
}

func summarize(content string) string {
	var obj map[string]any
	if json.Unmarshal([]byte(content), &obj) != nil {
		return truncate(oneLine(content), 140)
	}
	switch {
	case obj["error"] != nil:
		return "error: " + truncate(oneLine(fmt.Sprint(obj["error"])), 140)
	case obj["total_lines"] != nil:
		return numText(obj["total_lines"]) + " lines"
	case obj["count"] != nil:
		return numText(obj["count"]) + " entries"
	case obj["bytes_written"] != nil:
		return "wrote " + numText(obj["bytes_written"]) + " bytes"
	case obj["exit_code"] != nil:
		out := truncate(oneLine(fmt.Sprint(obj["output"])), 120)
		if numText(obj["exit_code"]) != "0" {
			return "exit " + numText(obj["exit_code"]) + ": " + out
		}
		if out == "" {
			return "ok"
		}
		return out
	case obj["success"] != nil:
		if note, ok := obj["note"].(string); ok {
			return oneLine(note)
		}
		return "ok"
	}
	return truncate(oneLine(content), 140)
}
