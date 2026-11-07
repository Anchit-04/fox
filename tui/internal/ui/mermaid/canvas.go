package mermaid

import "strings"

const (
	up uint8 = 1 << iota
	down
	left
	right
)

var glyphs = map[uint8]rune{
	up: '│', down: '│', up | down: '│',
	left: '─', right: '─', left | right: '─',
	down | right: '┌', down | left: '┐', up | right: '└', up | left: '┘',
	up | down | right: '├', up | down | left: '┤', down | left | right: '┬', up | left | right: '┴',
	up | down | left | right: '┼',
}

type point struct{ x, y int }

type canvas struct {
	w, h  int
	text  [][]rune
	lines [][]uint8
	dash  [][]bool
}

func newCanvas(w, h int) *canvas {
	c := &canvas{w: w, h: h}
	c.text = make([][]rune, h)
	c.lines = make([][]uint8, h)
	c.dash = make([][]bool, h)
	for y := range h {
		c.text[y] = make([]rune, w)
		c.lines[y] = make([]uint8, w)
		c.dash[y] = make([]bool, w)
	}
	return c
}

func (c *canvas) inside(x, y int) bool {
	return x >= 0 && y >= 0 && x < c.w && y < c.h
}

func (c *canvas) put(x, y int, r rune) {
	if c.inside(x, y) {
		c.text[y][x] = r
	}
}

func (c *canvas) write(x, y int, s string) {
	for _, r := range s {
		c.put(x, y, r)
		x++
	}
}

func (c *canvas) link(a, b point, dashed bool) {
	for a != b {
		next := a
		switch {
		case b.x > a.x:
			next.x++
			c.mark(a, right, dashed)
			c.mark(next, left, dashed)
		case b.x < a.x:
			next.x--
			c.mark(a, left, dashed)
			c.mark(next, right, dashed)
		case b.y > a.y:
			next.y++
			c.mark(a, down, dashed)
			c.mark(next, up, dashed)
		default:
			next.y--
			c.mark(a, up, dashed)
			c.mark(next, down, dashed)
		}
		a = next
	}
}

func (c *canvas) path(points []point, dashed bool) {
	for i := 1; i < len(points); i++ {
		c.link(points[i-1], points[i], dashed)
	}
}

func (c *canvas) mark(p point, dir uint8, dashed bool) {
	if c.inside(p.x, p.y) {
		c.lines[p.y][p.x] |= dir
		c.dash[p.y][p.x] = c.dash[p.y][p.x] || dashed
	}
}

func (c *canvas) String() string {
	rows := make([]string, 0, c.h)
	for y := range c.h {
		var b strings.Builder
		for x := range c.w {
			switch {
			case c.text[y][x] != 0:
				b.WriteRune(c.text[y][x])
			case c.lines[y][x] != 0:
				b.WriteRune(glyph(c.lines[y][x], c.dash[y][x]))
			default:
				b.WriteByte(' ')
			}
		}
		rows = append(rows, strings.TrimRight(b.String(), " "))
	}
	for len(rows) > 0 && rows[len(rows)-1] == "" {
		rows = rows[:len(rows)-1]
	}
	return strings.Join(rows, "\n")
}

func glyph(mask uint8, dashed bool) rune {
	if dashed {
		switch mask {
		case up, down, up | down:
			return '┊'
		case left, right, left | right:
			return '┄'
		}
	}
	return glyphs[mask]
}

func width(s string) int {
	return len([]rune(s))
}
