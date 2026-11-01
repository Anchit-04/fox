package transcript

import (
	"regexp"
	"strings"

	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"

	"fox/tui/internal/ui/theme"
)

var (
	headingRe = regexp.MustCompile(`^#{1,6}\s+(.*)$`)
	ruleRe    = regexp.MustCompile(`^(-{3,}|\*{3,}|_{3,})$`)
	bulletRe  = regexp.MustCompile(`^(\s*)[-*+]\s+(.*)$`)
	orderedRe = regexp.MustCompile(`^(\s*)(\d+[.)])\s+(.*)$`)
	quoteRe   = regexp.MustCompile(`^>\s?(.*)$`)
	linkRe    = regexp.MustCompile(`^\[([^\]]+)\]\(([^)\s]+)\)`)
)

func markdown(th *theme.Theme, text string, width int) string {
	width = max(width, minWidth)
	var out []string
	inCode := false
	for _, line := range strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "```") {
			inCode = !inCode
			continue
		}
		if inCode {
			code := strings.ReplaceAll(line, "\t", "    ")
			out = append(out, th.Rule.Render("│ ")+th.Code.Render(ansi.Truncate(code, width-2, "…")))
			continue
		}
		if m := headingRe.FindStringSubmatch(trimmed); m != nil {
			out = append(out, wrap(inline(th, m[1], th.Strong), width))
			continue
		}
		if m := bulletRe.FindStringSubmatch(line); m != nil {
			out = append(out, hang(indent(m[1])+th.Accent.Render("• "), inline(th, m[2], th.Text), width))
			continue
		}
		if m := orderedRe.FindStringSubmatch(line); m != nil {
			out = append(out, hang(indent(m[1])+th.Accent.Render(m[2]+" "), inline(th, m[3], th.Text), width))
			continue
		}
		switch m := quoteRe.FindStringSubmatch(trimmed); {
		case m != nil:
			out = append(out, hang(th.Rule.Render("│ "), inline(th, m[1], th.Muted), width))
		case ruleRe.MatchString(trimmed):
			out = append(out, th.Rule.Render(strings.Repeat("─", width)))
		case trimmed == "":
			out = append(out, "")
		default:
			out = append(out, wrap(inline(th, trimmed, th.Text), width))
		}
	}
	return strings.Join(out, "\n")
}

func inline(th *theme.Theme, s string, base lipgloss.Style) string {
	var b, run strings.Builder
	bold, italic := false, false
	flush := func() {
		if run.Len() > 0 {
			b.WriteString(base.Bold(bold || base.GetBold()).Italic(italic).Render(run.String()))
			run.Reset()
		}
	}
	for i := 0; i < len(s); {
		rest := s[i:]
		switch {
		case rest[0] == '`':
			if j := strings.IndexByte(rest[1:], '`'); j >= 0 {
				flush()
				b.WriteString(th.Code.Render(rest[1 : j+1]))
				i += j + 2
				continue
			}
		case strings.HasPrefix(rest, "**"):
			if bold || strings.Contains(rest[2:], "**") {
				flush()
				bold = !bold
				i += 2
				continue
			}
		case rest[0] == '*':
			if italic || (len(rest) > 1 && rest[1] != ' ' && !wordBefore(s, i) && strings.Contains(rest[1:], "*")) {
				flush()
				italic = !italic
				i++
				continue
			}
		case rest[0] == '[':
			if m := linkRe.FindStringSubmatch(rest); m != nil {
				flush()
				b.WriteString(th.Accent.Underline(true).Render(m[1]))
				i += len(m[0])
				continue
			}
		}
		run.WriteByte(s[i])
		i++
	}
	flush()
	return b.String()
}

func wordBefore(s string, i int) bool {
	if i == 0 {
		return false
	}
	c := s[i-1]
	return c == '_' || c >= '0' && c <= '9' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z'
}

func wrap(s string, width int) string {
	return lipgloss.NewStyle().Width(width).Render(s)
}

func hang(prefix, body string, width int) string {
	pw := lipgloss.Width(prefix)
	lines := strings.Split(wrap(body, max(width-pw, 1)), "\n")
	pad := strings.Repeat(" ", pw)
	for i := range lines {
		if i == 0 {
			lines[i] = prefix + lines[i]
		} else {
			lines[i] = pad + lines[i]
		}
	}
	return strings.Join(lines, "\n")
}

func indent(lead string) string {
	return strings.Repeat(" ", len(strings.ReplaceAll(lead, "\t", "  ")))
}
