package splash

import (
	"strings"

	"charm.land/lipgloss/v2"

	"fox/tui/internal/ui/theme"
)

var wordmark = []string{
	"11111 01110 10001",
	"10000 10001 01010",
	"11110 10001 00100",
	"10000 10001 01010",
	"10000 01110 10001",
}

type Info struct {
	Version    string
	Workspace  string
	Model      string
	Mode       string
	Permission string
}

func on(row string, i int) bool {
	return i < len(row) && row[i] == '1'
}

func Large(th *theme.Theme) string {
	lines := make([]string, len(wordmark))
	for r, row := range wordmark {
		var b strings.Builder
		for i := range len(row) {
			if on(row, i) {
				b.WriteString("██")
			} else {
				b.WriteString("  ")
			}
		}
		lines[r] = b.String()
	}
	return th.Accent.Render(strings.Join(lines, "\n"))
}

func Small(th *theme.Theme) string {
	var lines []string
	for r := 0; r < len(wordmark); r += 2 {
		top, bottom := wordmark[r], ""
		if r+1 < len(wordmark) {
			bottom = wordmark[r+1]
		}
		var b strings.Builder
		for i := range len(top) {
			switch up, down := on(top, i), on(bottom, i); {
			case up && down:
				b.WriteString("█")
			case up:
				b.WriteString("▀")
			case down:
				b.WriteString("▄")
			default:
				b.WriteString(" ")
			}
		}
		lines = append(lines, b.String())
	}
	return th.Accent.Render(strings.Join(lines, "\n"))
}

func Banner(th *theme.Theme, version string) string {
	text := th.Strong.Render("fox") + th.Faint.Render(" "+version) + "\n" + th.Muted.Render("multi-agent coding harness")
	return lipgloss.JoinHorizontal(lipgloss.Bottom, Small(th), "   ", text)
}

func View(th *theme.Theme, width, height int, info Info) string {
	title := th.Strong.Render("fox") + th.Faint.Render(" "+info.Version) + th.Muted.Render(" · multi-agent coding harness")

	var details []string
	for _, s := range []string{info.Workspace, info.Model, info.Mode, info.Permission} {
		if s != "" {
			details = append(details, s)
		}
	}
	hints := "enter send · alt+enter newline · tab sidebar · shift+tab mode · ctrl+p model · ctrl+k vault"
	fit := func(st lipgloss.Style, s string) string {
		return st.Width(min(lipgloss.Width(s), width)).Align(lipgloss.Center).Render(s)
	}

	block := lipgloss.JoinVertical(lipgloss.Center,
		Large(th),
		"",
		title,
		"",
		fit(th.Muted, strings.Join(details, " · ")),
		"",
		fit(th.Faint, hints),
	)
	return lipgloss.Place(width, height, lipgloss.Center, lipgloss.Center, block)
}
