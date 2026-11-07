package theme

import (
	"image/color"
	"slices"

	"charm.land/lipgloss/v2"
)

const Default = "light"

type Palette struct {
	Background string
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
	"light": {
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
	Selection  color.Color
	Ink        color.Color
	Dim        color.Color
	Line       color.Color
	Edge       color.Color
	Primary    color.Color
	UserBg     color.Color
	Alert      color.Color

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
	t := &Theme{
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

func (t *Theme) Box(focused bool) lipgloss.Style {
	border := t.Edge
	if focused {
		border = t.Dim
	}
	return lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(border).Padding(0, 1)
}
