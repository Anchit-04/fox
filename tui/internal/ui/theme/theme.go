package theme

import (
	"fmt"
	"hash/fnv"
	"image/color"
	"slices"
	"strings"

	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"
)

const Default = "midnight"

const (
	ClaudeCode = "claude-code"
	Codex      = "codex"
)

const onColour = "#0d0d0d"

type Palette struct {
	ClaudeCode string
	Codex      string
	Pool       []string

	Background string
	Panel      string
	Card       string
	Selection  string
	Ink        string
	Dim        string
	Faint      string
	Line       string
	Edge       string
	Accent     string
	UserBg     string
	Code       string
	Alert      string
	Added      string
	Removed    string
}

var Palettes = map[string]Palette{
	"midnight": {
		Background: "#0b0f17",
		Panel:      "#121825",
		Card:       "#1a2233",
		Selection:  "#24304d",
		Ink:        "#dfe5ee",
		Dim:        "#7a869c",
		Faint:      "#4a556c",
		Line:       "#1f2738",
		Edge:       "#2c3750",
		Accent:     "#6ea0ff",
		UserBg:     "#151c2b",
		Code:       "#f2c14e",
		Alert:      "#ff5c5c",
		Added:      "#3ecf8e",
		Removed:    "#ff5c5c",
		ClaudeCode: "#ff7a45",
		Codex:      "#3ecf8e",
		Pool:       []string{"#6ea0ff", "#f2c14e", "#d58cff", "#4fd1d9"},
	},
	"signal": {
		Background: "#0d0d0d",
		Selection:  "#1c1c1c",
		Ink:        "#e9e6df",
		Dim:        "#8a877f",
		Faint:      "#5c5a55",
		Line:       "#2a2a2a",
		Edge:       "#3a3a3a",
		Accent:     "#e9e6df",
		UserBg:     "#1a1a1a",
		Code:       "#e0b84a",
		Alert:      "#ff4d4d",
		Added:      "#19c37d",
		Removed:    "#ff4d4d",
		ClaudeCode: "#ff5a1f",
		Codex:      "#19c37d",
		Pool:       []string{"#3d7eff", "#ffc21a", "#ff4fa3", "#2bd4d9"},
	},
	"light": {
		ClaudeCode: "#d9480f",
		Codex:      "#0f8a5f",
		Pool:       []string{"#1f5fd6", "#a06a00", "#c2297a", "#0b7f8a"},
		Background: "#d9d8d4",
		Selection:  "#cac9c4",
		Ink:        "#1c1c1c",
		Dim:        "#605e5a",
		Faint:      "#7a7873",
		Line:       "#c4c2bd",
		Edge:       "#b3b1ac",
		Accent:     "#141414",
		UserBg:     "#cfcec9",
		Code:       "#7d5c1d",
		Alert:      "#b23b2e",
		Added:      "#2e6b34",
		Removed:    "#a8342a",
	},
	"ochre": {
		ClaudeCode: "#ff5a1f",
		Codex:      "#19c37d",
		Pool:       []string{"#3d7eff", "#ffc21a", "#ff4fa3", "#2bd4d9"},
		Background: "#1e1e1e",
		Selection:  "#28251f",
		Ink:        "#d9d6cf",
		Dim:        "#8b8882",
		Faint:      "#5f5d59",
		Line:       "#2d2d2d",
		Edge:       "#3a3a3a",
		Accent:     "#d4a24c",
		UserBg:     "#262626",
		Code:       "#b4c0cc",
		Alert:      "#d0614f",
		Added:      "#86b36b",
		Removed:    "#d0614f",
	},
}

func Names() []string {
	names := make([]string, 0, len(Palettes))
	for name := range Palettes {
		names = append(names, name)
	}
	slices.Sort(names)
	return names
}

type Theme struct {
	Name       string
	Background color.Color
	Panel      color.Color
	Card       color.Color
	Selection  color.Color
	Ink        color.Color
	Dim        color.Color
	Line       color.Color
	Edge       color.Color
	Primary    color.Color
	UserBg     color.Color
	Alert      color.Color

	claudeCode color.Color
	codex      color.Color
	pool       []color.Color

	Text     lipgloss.Style
	Muted    lipgloss.Style
	Faint    lipgloss.Style
	Accent   lipgloss.Style
	Strong   lipgloss.Style
	Heading  lipgloss.Style
	Selected lipgloss.Style
	Rule     lipgloss.Style
	Code     lipgloss.Style
	Warn     lipgloss.Style
	Added    lipgloss.Style
	Removed  lipgloss.Style
}

func New(name, background string) *Theme {
	p, ok := Palettes[name]
	if !ok {
		name, p = Default, Palettes[Default]
	}
	bg := lipgloss.Color(background)
	if _, invalid := bg.(lipgloss.NoColor); invalid {
		bg = lipgloss.Color(p.Background)
	}
	if p.Panel == "" {
		p.Panel = p.Selection
	}
	if p.Card == "" {
		p.Card = p.UserBg
	}
	t := &Theme{
		Panel:      lipgloss.Color(p.Panel),
		Card:       lipgloss.Color(p.Card),
		Name:       name,
		Background: bg,
		Selection:  lipgloss.Color(p.Selection),
		Ink:        lipgloss.Color(p.Ink),
		Dim:        lipgloss.Color(p.Dim),
		Line:       lipgloss.Color(p.Line),
		Edge:       lipgloss.Color(p.Edge),
		Primary:    lipgloss.Color(p.Accent),
		UserBg:     lipgloss.Color(p.UserBg),
		Alert:      lipgloss.Color(p.Alert),
		claudeCode: lipgloss.Color(p.ClaudeCode),
		codex:      lipgloss.Color(p.Codex),
	}
	for _, c := range p.Pool {
		t.pool = append(t.pool, lipgloss.Color(c))
	}
	t.Text = lipgloss.NewStyle().Foreground(t.Ink)
	t.Muted = lipgloss.NewStyle().Foreground(t.Dim)
	t.Faint = lipgloss.NewStyle().Foreground(lipgloss.Color(p.Faint))
	t.Accent = lipgloss.NewStyle().Foreground(t.Primary)
	t.Strong = lipgloss.NewStyle().Foreground(t.Ink).Bold(true)
	t.Heading = t.Faint
	t.Selected = lipgloss.NewStyle().Background(t.Selection)
	t.Rule = lipgloss.NewStyle().Foreground(t.Line)
	t.Code = lipgloss.NewStyle().Foreground(lipgloss.Color(p.Code))
	t.Warn = lipgloss.NewStyle().Foreground(t.Alert)
	t.Added = lipgloss.NewStyle().Foreground(lipgloss.Color(p.Added))
	t.Removed = lipgloss.NewStyle().Foreground(lipgloss.Color(p.Removed))
	return t
}

func (t *Theme) Worker(key string) color.Color {
	switch key {
	case "":
		return t.Dim
	case ClaudeCode:
		return t.claudeCode
	case Codex:
		return t.codex
	}
	if len(t.pool) == 0 {
		return t.Dim
	}
	h := fnv.New32a()
	h.Write([]byte(key))
	return t.pool[h.Sum32()%uint32(len(t.pool))]
}

func (t *Theme) Badge(key string) lipgloss.Style {
	return lipgloss.NewStyle().Background(t.Worker(key)).Foreground(lipgloss.Color(onColour)).Bold(true)
}

func (t *Theme) Tint(key string) lipgloss.Style {
	return lipgloss.NewStyle().Foreground(t.Worker(key))
}

const reset = "\x1b[m"

func Fill(s string, bg color.Color, width int) string {
	r, g, b, _ := bg.RGBA()
	on := fmt.Sprintf("\x1b[48;2;%d;%d;%dm", r>>8, g>>8, b>>8)
	lines := strings.Split(s, "\n")
	for i, line := range lines {
		pad := max(width-ansi.StringWidth(line), 0)
		line = strings.ReplaceAll(line, "\x1b[0m", reset)
		line = strings.ReplaceAll(line, reset, reset+on)
		lines[i] = on + line + strings.Repeat(" ", pad) + reset
	}
	return strings.Join(lines, "\n")
}

func (t *Theme) Box(focused bool) lipgloss.Style {
	border := t.Edge
	if focused {
		border = t.Dim
	}
	return lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(border).Padding(0, 1)
}
