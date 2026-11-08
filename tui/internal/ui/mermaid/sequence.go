package mermaid

import (
	"fmt"
	"regexp"
	"strings"
)

var (
	seqMessage = regexp.MustCompile(`^([^\s:<>+-][^:<>]*?)\s*(-{1,2}>>|-{1,2}>|-{1,2}x|-{1,2}\))\s*([+-]?)([^:]+?)\s*:\s*(.*)$`)
	seqNote    = regexp.MustCompile(`(?i)^note\s+(over|left of|right of)\s+([^:]+?)\s*:\s*(.*)$`)
	seqActor   = regexp.MustCompile(`^(participant|actor)\s+(.+?)(?:\s+as\s+(.+))?$`)
	seqBlock   = regexp.MustCompile(`^(loop|alt|else|opt|par|and|critical|break|rect)\b\s*(.*)$`)
)

type seqStep struct {
	kind   string
	from   int
	to     int
	text   string
	dashed bool
	cross  bool
	head   bool
}

type sequence struct {
	names  []string
	labels []string
	index  map[string]int
	steps  []seqStep
	number bool
}

func (s *sequence) actor(name string) int {
	name = strings.TrimSpace(name)
	if i, ok := s.index[name]; ok {
		return i
	}
	s.index[name] = len(s.names)
	s.names = append(s.names, name)
	s.labels = append(s.labels, name)
	return len(s.names) - 1
}

func parseSequence(body []string) (*sequence, error) {
	s := &sequence{index: map[string]int{}}
	for n, raw := range body {
		line := strings.TrimSpace(raw)
		switch {
		case line == "" || strings.HasPrefix(line, "%%") || line == "end" || strings.HasPrefix(line, "activate") ||
			strings.HasPrefix(line, "deactivate") || strings.HasPrefix(line, "title"):
		case line == "autonumber":
			s.number = true
		case seqActor.MatchString(line):
			m := seqActor.FindStringSubmatch(line)
			i := s.actor(m[2])
			if m[3] != "" {
				s.labels[i] = strings.TrimSpace(m[3])
			}
		case seqNote.MatchString(line):
			m := seqNote.FindStringSubmatch(line)
			who := strings.Split(m[2], ",")
			a := s.actor(who[0])
			b := a
			if len(who) > 1 {
				b = s.actor(who[1])
			}
			s.steps = append(s.steps, seqStep{kind: "note " + strings.ToLower(m[1]), from: a, to: b, text: m[3]})
		case seqBlock.MatchString(line):
			m := seqBlock.FindStringSubmatch(line)
			s.steps = append(s.steps, seqStep{kind: "block", text: strings.TrimSpace(m[1] + " " + m[2])})
		case seqMessage.MatchString(line):
			m := seqMessage.FindStringSubmatch(line)
			arrow := m[2]
			s.steps = append(s.steps, seqStep{kind: "message", from: s.actor(m[1]), to: s.actor(m[4]), text: m[5],
				dashed: strings.HasPrefix(arrow, "--"), cross: strings.HasSuffix(arrow, "x"),
				head: strings.Contains(arrow, ">")})
		default:
			return nil, fmt.Errorf("line %d: can't read %q", n+2, ansiCut(line, 30))
		}
	}
	if len(s.names) == 0 {
		return nil, fmt.Errorf("the sequence diagram has no participants")
	}
	return s, nil
}

func (s *sequence) render() string {
	count := len(s.names)
	boxes := make([]int, count)
	for i, l := range s.labels {
		boxes[i] = width(l) + 4
	}
	gaps := make([]int, max(count-1, 0))
	for i := range gaps {
		gaps[i] = (boxes[i]+1)/2 + boxes[i+1]/2 + 3
	}
	number := 0
	texts := make([]string, len(s.steps))
	for i, st := range s.steps {
		text := ansiCut(st.text, 40)
		if st.kind == "message" && s.number {
			number++
			text = fmt.Sprintf("%d. %s", number, text)
		}
		texts[i] = text
		a, b := min(st.from, st.to), max(st.from, st.to)
		need := width(text) + 4
		if st.kind == "message" && a == b {
			if a < len(gaps) {
				gaps[a] = max(gaps[a], width(text)+8)
			}
			continue
		}
		if st.kind != "message" && st.kind != "note over" {
			continue
		}
		have := 0
		for g := a; g < b; g++ {
			have += gaps[g]
		}
		if a < b && have < need {
			gaps[b-1] += need - have
		}
	}
	centers := make([]int, count)
	centers[0] = boxes[0] / 2
	for i := 1; i < count; i++ {
		centers[i] = centers[i-1] + gaps[i-1]
	}
	right := centers[count-1] + boxes[count-1]/2 + 2
	for i, st := range s.steps {
		if st.kind == "message" && st.from == st.to {
			right = max(right, centers[st.from]+width(texts[i])+8)
		}
		if strings.HasPrefix(st.kind, "note") {
			right = max(right, centers[max(st.from, st.to)]+width(texts[i])+6)
		}
	}
	rows := 3
	for _, st := range s.steps {
		switch {
		case st.kind == "block":
			rows++
		case st.kind == "message" && st.from == st.to:
			rows += 3
		case st.kind == "message":
			rows += 2
		default:
			rows += 3
		}
	}
	height := rows + 3
	c := newCanvas(right+1, height)
	for i := range count {
		c.path([]point{{centers[i], 3}, {centers[i], height - 4}}, true)
	}
	drawHead := func(i, y int) {
		x := centers[i] - boxes[i]/2
		c.write(x, y, "┌"+strings.Repeat("─", boxes[i]-2)+"┐")
		c.write(x, y+1, "│ "+s.labels[i]+" │")
		c.write(x, y+2, "└"+strings.Repeat("─", boxes[i]-2)+"┘")
	}
	y := 3
	for i, st := range s.steps {
		text := texts[i]
		switch {
		case st.kind == "block":
			c.write(1, y, "▸ "+text)
			y++
		case st.kind == "message" && st.from == st.to:
			x := centers[st.from]
			c.write(x+1, y, "──┐ "+text)
			c.write(x+1, y+1, "  │")
			c.write(x+1, y+2, "◄─┘")
			y += 3
		case st.kind == "message":
			a, b := centers[st.from], centers[st.to]
			lo, hi := min(a, b), max(a, b)
			c.write(lo+(hi-lo-width(text))/2+1, y, text)
			line := '─'
			if st.dashed {
				line = '┄'
			}
			for x := lo + 1; x < hi; x++ {
				c.put(x, y+1, line)
			}
			head := '►'
			if b < a {
				head = '◄'
			}
			if st.cross {
				head = '×'
			}
			if st.head || st.cross {
				if b > a {
					c.put(hi-1, y+1, head)
				} else {
					c.put(lo+1, y+1, head)
				}
			}
			y += 2
		default:
			lo, hi := centers[min(st.from, st.to)], centers[max(st.from, st.to)]
			w := width(text) + 4
			x := lo - w/2
			switch st.kind {
			case "note left of":
				x = lo - w - 1
			case "note right of":
				x = lo + 2
			case "note over":
				if hi > lo {
					x = lo
					w = max(w, hi-lo+1)
				}
			}
			x = max(x, 0)
			c.write(x, y, "╭"+strings.Repeat("─", w-2)+"╮")
			c.write(x, y+1, "│ "+text+strings.Repeat(" ", max(w-4-width(text), 0))+" │")
			c.write(x, y+2, "╰"+strings.Repeat("─", w-2)+"╯")
			y += 3
		}
	}
	for i := range count {
		drawHead(i, 0)
		drawHead(i, height-3)
	}
	return c.String()
}
