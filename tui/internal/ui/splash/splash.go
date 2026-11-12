package splash

import (
	"image/color"
	"strings"

	"charm.land/lipgloss/v2"

	"fox/tui/internal/ui/theme"
)

var mark = []string{
	"10000000001",
	"11000000011",
	"11100000111",
	"11111111111",
	"11111111111",
	"11311111311",
	"02211111220",
	"00222122200",
	"00002320000",
}

type Info struct {
	Version    string
	Workspace  string
	Model      string
	Mode       string
	Permission string
}

func cell(row string, i int) byte {
	if i < len(row) && (row[i] == '1' || row[i] == '2') {
		return row[i]
	}
	return 0
}

func paint(th *theme.Theme, c byte) color.Color {
	if c == '2' {
		return th.Ink
	}
	return th.Primary
}

func Large(th *theme.Theme) string {
	lines := make([]string, len(mark))
	for r, row := range mark {
		var b strings.Builder
		for i := range len(row) {
			if c := cell(row, i); c != 0 {
				b.WriteString(lipgloss.NewStyle().Foreground(paint(th, c)).Render("██"))
			} else {
				b.WriteString("  ")
			}
		}
		lines[r] = b.String()
	}
	return strings.Join(lines, "\n")
}

func Small(th *theme.Theme) string {
	var lines []string
	for r := 0; r < len(mark); r += 2 {
		top, bottom := mark[r], ""
		if r+1 < len(mark) {
			bottom = mark[r+1]
		}
		var b strings.Builder
		for i := range len(top) {
			up, down := cell(top, i), cell(bottom, i)
			switch {
			case up == 0 && down == 0:
				b.WriteString(" ")
			case down == 0:
				b.WriteString(lipgloss.NewStyle().Foreground(paint(th, up)).Render("▀"))
			case up == 0:
				b.WriteString(lipgloss.NewStyle().Foreground(paint(th, down)).Render("▄"))
			case up == down:
				b.WriteString(lipgloss.NewStyle().Foreground(paint(th, up)).Render("█"))
			default:
				b.WriteString(lipgloss.NewStyle().Foreground(paint(th, up)).Background(paint(th, down)).Render("▀"))
			}
		}
		lines = append(lines, b.String())
	}
	return strings.Join(lines, "\n")
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
