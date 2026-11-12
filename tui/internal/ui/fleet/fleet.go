package fleet

import (
	"fmt"
	"strings"
	"time"

	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"

	"fox/tui/internal/state"
	"fox/tui/internal/ui/layout"
	"fox/tui/internal/ui/theme"
	"fox/tui/internal/ui/transcript"
)

const (
	BudgetRows = 2
	growSlack  = 5
	stripRows  = 2
	maxBar     = 60
	reportRows = 8
)

var spinner = []string{"◐", "◓", "◑", "◒"}

type hit struct {
	x0, x1 int
	id     string
}

type Model struct {
	theme *theme.Theme
	area  layout.Rect
	cols  int
	count int
	cells []layout.Rect
	shown []string
	rest  []hit
	strip layout.Rect
	focus string
	page  int
	pages int
}

func New(th *theme.Theme) Model {
	return Model{theme: th}
}

func (m *Model) Focused() string { return m.focus }

func (m *Model) Blur() { m.focus = "" }

func (m *Model) Next(sess *state.Session) {
	if sess == nil || len(sess.TaskOrder) == 0 {
		m.focus = ""
		return
	}
	for i, id := range sess.TaskOrder {
		if id == m.focus {
			if i+1 < len(sess.TaskOrder) {
				m.focus = sess.TaskOrder[i+1]
			} else {
				m.focus = ""
			}
			return
		}
	}
	m.focus = sess.TaskOrder[0]
}

func (m *Model) columns(n int, area layout.Rect) int {
	fit, _ := layout.GridFit(area)
	cols := min(layout.GridCols(n), fit)
	grew := m.cols > 0 && m.count == n && cols > m.cols
	if grew && area.W < cols*(layout.MinPaneW+1)+growSlack {
		cols = m.cols
	}
	m.cols, m.count = cols, n
	return cols
}

func (m *Model) Layout(sess *state.Session, area layout.Rect) {
	m.area = area
	m.cells, m.shown, m.rest, m.strip, m.pages = nil, nil, nil, layout.Rect{}, 1
	if sess == nil || len(sess.TaskOrder) == 0 || area.W <= 0 || area.H <= 0 {
		m.focus = ""
		return
	}
	if m.focus != "" && sess.Tasks[m.focus] == nil {
		m.focus = ""
	}
	ids := sess.TaskOrder
	cols := m.columns(len(ids), area)
	_, rows := layout.GridFit(area)
	if len(ids) <= cols*rows {
		m.shown = ids
		m.cells = layout.Grid(area, len(ids), cols)
		return
	}

	grid := area
	if area.H > layout.MinPaneH+stripRows {
		parts := layout.SplitV(area, 0, 1, 1)
		grid, m.strip = parts[0], parts[2]
	}
	_, rows = layout.GridFit(grid)
	capacity := cols * rows
	var live, done []string
	for _, id := range ids {
		if sess.Tasks[id].Finished() {
			done = append(done, id)
		} else {
			live = append(live, id)
		}
	}
	var left []string
	if len(live) > capacity {
		m.pages = (len(live) + capacity - 1) / capacity
		for i, id := range live {
			if id == m.focus {
				m.page = i / capacity
			}
		}
		m.page = min(max(m.page, 0), m.pages-1)
		m.shown = live[m.page*capacity : min((m.page+1)*capacity, len(live))]
		left = done
	} else {
		all := append(append([]string{}, live...), done...)
		m.shown, left = all[:capacity], all[capacity:]
	}
	m.cells = layout.Grid(grid, len(m.shown), cols)
	x := m.strip.X
	for _, id := range left {
		w := lipgloss.Width(stripLabel(sess.Tasks[id]))
		m.rest = append(m.rest, hit{x0: x, x1: x + w, id: id})
		x += w + 3
	}
}

func (m Model) TaskAt(x, y int) (string, bool) {
	for i, c := range m.cells {
		if c.Contains(x, y) && i < len(m.shown) {
			return m.shown[i], true
		}
	}
	if m.strip.Contains(x, y) {
		for _, h := range m.rest {
			if x >= h.x0 && x < h.x1 {
				return h.id, true
			}
		}
	}
	return "", false
}

func (m Model) View(sess *state.Session, now time.Time) string {
	th := m.theme
	box := lipgloss.NewStyle().Width(m.area.W).Height(m.area.H).MaxWidth(m.area.W).MaxHeight(m.area.H)
	if sess == nil || len(m.cells) == 0 {
		return box.Align(lipgloss.Center, lipgloss.Center).Render(th.Faint.Render("no agents yet"))
	}
	var bands []string
	for i := 0; i < len(m.cells); i += m.cols {
		var row []string
		for j := i; j < min(i+m.cols, len(m.cells)); j++ {
			if j > i {
				row = append(row, " ")
			}
			id := m.shown[j]
			row = append(row, m.pane(sess, sess.Tasks[id], m.cells[j], id == m.focus, now))
		}
		if i > 0 {
			bands = append(bands, "")
		}
		bands = append(bands, lipgloss.JoinHorizontal(lipgloss.Top, row...))
	}
	if m.strip.H > 0 {
		for lipgloss.Height(strings.Join(bands, "\n")) < m.strip.Y-m.area.Y {
			bands = append(bands, "")
		}
		bands = append(bands, m.stripView(sess))
	}
	return box.Render(strings.Join(bands, "\n"))
}

func stripLabel(t *state.Task) string {
	return "■ " + strings.ToUpper(t.ID) + " " + worker(t) + " " + word(t.Status)
}

func (m Model) stripView(sess *state.Session) string {
	th := m.theme
	var parts []string
	for _, h := range m.rest {
		t := sess.Tasks[h.id]
		text := stripLabel(t)
		mark, rest, _ := strings.Cut(text, " ")
		parts = append(parts, th.Tint(t.Model).Render(mark)+" "+th.Muted.Render(rest))
	}
	line := strings.Join(parts, "   ")
	if m.pages > 1 {
		line += th.Faint.Render(fmt.Sprintf("   page %d/%d · tab moves on", m.page+1, m.pages))
	}
	return ansi.Truncate(line, m.area.W, "…")
}

func worker(t *state.Task) string {
	if t.Model == "" {
		return "worker"
	}
	return t.Model
}

func word(status string) string {
	switch status {
	case "running":
		return "RUN"
	case "needs-approval":
		return "WAIT"
	case "done":
		return "DONE"
	case "failed":
		return "FAIL"
	case "cancelled":
		return "STOP"
	case "escalated":
		return "ASKED"
	}
	return "QUEUED"
}

func (m Model) bar(t *state.Task, width int, now time.Time) string {
	th := m.theme
	left := " " + strings.ToUpper(t.ID) + " " + strings.ToUpper(worker(t))
	right := word(t.Status) + " "
	if t.Status == "running" {
		right = word(t.Status) + " " + spinner[int(now.Unix())%len(spinner)] + " "
	}
	left = ansi.Truncate(left, max(width-lipgloss.Width(right)-1, 1), "…")
	gap := strings.Repeat(" ", max(width-lipgloss.Width(left)-lipgloss.Width(right), 1))
	switch {
	case t.Status == "needs-approval":
		return lipgloss.NewStyle().Background(th.Alert).Foreground(th.Background).Bold(true).Render(left + gap + right)
	case t.Status == "running":
		return th.Badge(t.Model).Render(left + gap + right)
	case t.Finished():
		return th.Tint(t.Model).Bold(true).Render(left) + gap + th.Muted.Render(right)
	}
	return th.Muted.Render(left + gap + right)
}

func (m Model) footer(t *state.Task, now time.Time) string {
	parts := []string{Clock(t.Elapsed(now))}
	if t.Steps == 1 {
		parts = append(parts, "1 step")
	} else if t.Steps > 1 {
		parts = append(parts, fmt.Sprintf("%d steps", t.Steps))
	}
	if tokens := t.InputTokens + t.OutputTokens; tokens > 0 {
		parts = append(parts, Compact(tokens)+" tok")
	}
	if t.Cost > 0 {
		parts = append(parts, fmt.Sprintf("$%.3f", t.Cost))
	}
	return strings.Join(parts, " · ")
}

func wrap(style lipgloss.Style, text string, width, rows int) []string {
	if rows <= 0 || width <= 0 {
		return nil
	}
	lines := strings.Split(style.Width(width).Render(text), "\n")
	if len(lines) > rows {
		lines = lines[:rows]
		lines[rows-1] = ansi.Truncate(lines[rows-1], max(width-1, 1), "") + style.Render("…")
	}
	return lines
}

func (m Model) body(sess *state.Session, t *state.Task, width, rows int) []string {
	th := m.theme
	if rows <= 0 {
		return nil
	}
	lines := []string{th.Text.Render(ansi.Truncate(transcript.Label(t), width, "…"))}
	rows--
	switch {
	case t.Status == "failed" && t.Error != "":
		lines = append(lines, wrap(th.Warn, strings.Join(strings.Fields(t.Error), " "), width, rows)...)
	case t.Report != nil:
		if n := len(t.Report.Changes); n > 0 && rows > 1 {
			files := fmt.Sprintf("%d files changed", n)
			if n == 1 {
				files = "1 file changed"
			}
			lines = append(lines, th.Tint(t.Model).Render(files))
			rows--
		}
		text := plain(t.Report.Outcome)
		if text == "" {
			text = transcript.ReportLine(t.Report)
		}
		lines = append(lines, wrap(th.Muted, text, width, min(rows, reportRows))...)
	default:
		steps := transcript.Tail(sess.Entries, t.ID, rows)
		for i, s := range steps {
			style := th.Faint
			if i == len(steps)-1 {
				style = th.Muted
			}
			head := ""
			if s.Tool != "" {
				head = s.Tool + " "
			}
			if s.Count > 1 {
				head = fmt.Sprintf("%s ×%d ", s.Tool, s.Count)
			}
			s.Detail = short(s.Detail, sess.Workspace)
			switch {
			case s.Bad:
				lines = append(lines, th.Warn.Render(ansi.Truncate(head+s.Detail, width, "…")))
			case s.Open:
				lines = append(lines, th.Tint(t.Model).Render(ansi.Truncate(head+s.Detail, width, "…")))
			default:
				lines = append(lines, style.Render(ansi.Truncate(head+s.Detail, width, "…")))
			}
		}
		if len(steps) == 0 && !t.Finished() {
			lines = append(lines, th.Faint.Render("waiting to start"))
		}
	}
	return lines
}

func (m Model) pane(sess *state.Session, t *state.Task, r layout.Rect, focused bool, now time.Time) string {
	th := m.theme
	if t == nil {
		return lipgloss.NewStyle().Width(r.W).Height(r.H).Render("")
	}
	gutter := "  "
	if focused {
		gutter = th.Tint(t.Model).Render("▌") + " "
	}
	inner := max(r.W-2, 1)
	lines := []string{m.bar(t, r.W, now)}
	body := m.body(sess, t, inner, r.H-2)
	for len(body) < r.H-2 {
		body = append(body, "")
	}
	for _, l := range body {
		lines = append(lines, gutter+l)
	}
	lines = append(lines, gutter+th.Faint.Render(ansi.Truncate(m.footer(t, now), inner, "…")))
	return lipgloss.NewStyle().Width(r.W).Height(r.H).MaxWidth(r.W).MaxHeight(r.H).Render(strings.Join(lines, "\n"))
}

type share struct {
	name   string
	key    string
	cost   float64
	tokens int
}

func shares(sess *state.Session) []share {
	var out []share
	index := map[string]int{}
	cost, tokens := sess.Cost, sess.InputTokens+sess.OutputTokens
	for _, id := range sess.TaskOrder {
		t := sess.Tasks[id]
		name := worker(t)
		i, ok := index[name]
		if !ok {
			out = append(out, share{name: name, key: t.Model})
			i = len(out) - 1
			index[name] = i
		}
		out[i].cost += t.Cost
		out[i].tokens += t.InputTokens + t.OutputTokens
		cost -= t.Cost
		tokens -= t.InputTokens + t.OutputTokens
	}
	lead := share{name: "orchestrator", cost: max(cost, 0), tokens: max(tokens, 0)}
	return append([]share{lead}, out...)
}

func Budget(th *theme.Theme, sess *state.Session, width int) string {
	if sess == nil || width <= 0 {
		return ""
	}
	parts := shares(sess)
	byCost := sess.Cost > 0
	total := 0.0
	for _, p := range parts {
		if byCost {
			total += p.cost
		} else {
			total += float64(p.tokens)
		}
	}
	label := "COST   "
	if !byCost {
		label = "TOKENS "
	}
	room := min(max(width-lipgloss.Width(label)-2, 1), maxBar)
	var bar, legend strings.Builder
	used := 0
	for _, p := range parts {
		value := float64(p.tokens)
		amount := Compact(p.tokens) + " tok"
		if byCost {
			value = p.cost
			if p.cost > 0 || p.tokens == 0 {
				amount = fmt.Sprintf("$%.3f", p.cost)
			}
		}
		style := th.Tint(p.key)
		if p.key == "" {
			style = th.Text
		}
		if total > 0 && value > 0 {
			n := min(max(int(value/total*float64(room)+0.5), 1), room-used)
			bar.WriteString(style.Render(strings.Repeat("█", n)))
			used += n
		}
		legend.WriteString(style.Render("■") + " " + th.Muted.Render(p.name+" "+amount) + "   ")
	}
	bar.WriteString(th.Rule.Render(strings.Repeat("░", max(room-used, 0))))
	sum := fmt.Sprintf("TOTAL $%.3f · %s tok", sess.Cost, Compact(sess.InputTokens+sess.OutputTokens))
	if sess.CostPartial {
		sum = "TOTAL ≥" + strings.TrimPrefix(sum, "TOTAL ")
	}
	top := " " + th.Faint.Render(label) + bar.String()
	gap := max(width-lipgloss.Width(top)-lipgloss.Width(sum)-1, 1)
	top += strings.Repeat(" ", gap) + th.Text.Render(sum)
	bottom := " " + strings.Repeat(" ", lipgloss.Width(label)) + legend.String()
	return ansi.Truncate(top, width, "") + "\n" + ansi.Truncate(bottom, width, "…")
}

func Clock(d time.Duration) string {
	s := int(d / time.Second)
	if s >= 3600 {
		return fmt.Sprintf("%dh%02dm", s/3600, s%3600/60)
	}
	return fmt.Sprintf("%02d:%02d", s/60, s%60)
}

var markup = strings.NewReplacer("**", "", "`", "", "## ", "", "# ", "")

func plain(s string) string {
	return strings.Join(strings.Fields(markup.Replace(s)), " ")
}

func short(detail, workspace string) string {
	if workspace == "" {
		return detail
	}
	for _, root := range []string{workspace, strings.ReplaceAll(workspace, `\`, "/")} {
		for {
			i := strings.Index(strings.ToLower(detail), strings.ToLower(root))
			if i < 0 {
				break
			}
			rest := strings.TrimLeft(detail[i+len(root):], `\/`)
			detail = detail[:i] + rest
		}
	}
	return detail
}

func Compact(n int) string {
	switch {
	case n >= 1_000_000:
		return fmt.Sprintf("%.1fM", float64(n)/1_000_000)
	case n >= 1_000:
		return fmt.Sprintf("%.1fk", float64(n)/1_000)
	}
	return fmt.Sprint(n)
}
