package transcript

import (
	"fmt"
	"strings"

	"fox/tui/internal/state"
)

type block struct {
	text   string
	toggle string
}

type call struct {
	line   string
	tool   string
	failed bool
}

func isTrace(k state.EntryKind) bool {
	return k == state.ToolCallEntry || k == state.ToolResultEntry || k == state.DeniedEntry
}

func failed(e state.Entry) bool {
	if e.Kind == state.DeniedEntry {
		return true
	}
	s := summarize(e.Text)
	return strings.HasPrefix(s, "error: ") || strings.HasPrefix(s, "exit ") && !strings.HasPrefix(s, "exit 0")
}

func collect(entries []state.Entry, start int, task string) ([]call, int) {
	var calls []call
	i := start
	for ; i < len(entries) && entries[i].TaskID == task && isTrace(entries[i].Kind); i++ {
		e := entries[i]
		switch e.Kind {
		case state.ToolCallEntry:
			c := call{line: "→ " + e.Tool + "(" + formatArgs(e.Args) + ")", tool: e.Tool}
			if j, ok := nextResult(entries, i); ok && j < len(entries) {
				c.line += " · " + outcome(entries[j])
				c.failed = failed(entries[j])
				i = j
			}
			calls = append(calls, c)
		case state.DeniedEntry:
			calls = append(calls, call{line: "✗ denied " + e.Tool + ": " + oneLine(e.Text), tool: e.Tool, failed: true})
		default:
			calls = append(calls, call{line: "  " + summarize(e.Text), tool: e.Tool, failed: failed(e)})
		}
	}
	return calls, i - 1
}

func (m Model) traceBlock(calls []call, key string, width int) block {
	th := m.theme
	open := m.isOpen(key)
	arrow := "▸ "
	if open {
		arrow = "▾ "
	}
	noun := "tool calls"
	if len(calls) == 1 {
		noun = "tool call"
	}
	var names []string
	seen := map[string]bool{}
	bad := 0
	for _, c := range calls {
		if c.tool != "" && !seen[c.tool] {
			seen[c.tool] = true
			names = append(names, c.tool)
		}
		if c.failed {
			bad++
		}
	}
	head := th.Faint.Render(arrow + fmt.Sprintf("%d %s", len(calls), noun))
	if len(names) > 0 {
		head += th.Faint.Render(" · " + truncate(strings.Join(names, ", "), max(width-30, 10)))
	}
	if bad > 0 {
		head += th.Warn.Render(fmt.Sprintf(" · %d failed", bad))
	}
	if !open {
		return block{text: head, toggle: key}
	}
	lines := []string{head}
	for _, c := range calls {
		st := th.Faint
		if c.failed {
			st = th.Warn
		}
		lines = append(lines, st.Width(width).PaddingLeft(2).Render(c.line))
	}
	return block{text: strings.Join(lines, "\n"), toggle: key}
}

func (m Model) isOpen(key string) bool {
	return m.expandAll != m.open[key]
}

func join(blocks []block) (string, map[int]string) {
	toggles := map[int]string{}
	texts := make([]string, len(blocks))
	line := 0
	for i, b := range blocks {
		if b.toggle != "" {
			toggles[line] = b.toggle
		}
		texts[i] = b.text
		line += strings.Count(b.text, "\n") + 2
	}
	return strings.Join(texts, "\n\n"), toggles
}

func (m *Model) toggleAt(line int) bool {
	key, ok := m.toggles[line]
	if !ok {
		return false
	}
	m.open[key] = !m.open[key]
	m.dirty = true
	return true
}

func (m *Model) ToggleAll() {
	m.expandAll = !m.expandAll
	clear(m.open)
	m.dirty = true
}
