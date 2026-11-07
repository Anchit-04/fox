package mermaid

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
	"unicode"
)

type shape int

const (
	square shape = iota
	round
	diamond
)

const (
	maxLabel   = 24
	tdGap      = 3
	tdCrossGap = 4
	lrCrossGap = 1
	lrMinGap   = 8
)

type node struct {
	id    string
	lines []string
	shape shape
}

type edge struct {
	from, to   int
	label      string
	dashed     bool
	head, tail bool
}

type flow struct {
	dir   string
	nodes []*node
	index map[string]int
	edges []edge
}

var (
	skipWords = map[string]bool{"classDef": true, "class": true, "style": true, "linkStyle": true, "click": true,
		"subgraph": true, "end": true, "direction": true}
	edgeText  = regexp.MustCompile(`^(<)?(--|==|-\.)\s+([^|]+?)\s+(-->|---|==>|===|\.->|-\.->|--x|--o)`)
	edgePlain = regexp.MustCompile(`^(<)?(?:(-{2,}|={2,})(>|x|o)?|(-\.+-)(>)?)(?:\|([^|]*)\|)?`)
	shapes    = []struct {
		open, close string
		shape       shape
	}{
		{"(((", ")))", round}, {"((", "))", round}, {"([", "])", round}, {"[(", ")]", round},
		{"[[", "]]", square}, {"[/", "/]", square}, {`[\`, `\]`, square}, {"{{", "}}", diamond},
		{"[", "]", square}, {"(", ")", round}, {"{", "}", diamond}, {">", "]", square},
	}
)

func parseFlowchart(header string, body []string) (*flow, error) {
	f := &flow{dir: "TD", index: map[string]int{}}
	if head, rest, found := strings.Cut(header, ";"); found {
		header, body = head, append([]string{rest}, body...)
	}
	if fields := strings.Fields(header); len(fields) > 1 {
		switch strings.ToUpper(strings.TrimRight(fields[1], ";")) {
		case "LR", "RL":
			f.dir = "LR"
		}
	}
	for n, line := range body {
		for _, stmt := range strings.Split(line, ";") {
			stmt = strings.TrimSpace(stmt)
			if stmt == "" || strings.HasPrefix(stmt, "%%") || skipWords[strings.Fields(stmt)[0]] {
				continue
			}
			if err := f.parseChain(stmt); err != nil {
				return nil, fmt.Errorf("line %d: %w", n+2, err)
			}
		}
	}
	if len(f.nodes) == 0 {
		return nil, fmt.Errorf("the flowchart has no nodes")
	}
	return f, nil
}

type scanner struct {
	s []rune
	i int
}

func (sc *scanner) spaces() {
	for sc.i < len(sc.s) && unicode.IsSpace(sc.s[sc.i]) {
		sc.i++
	}
}

func (sc *scanner) rest() string { return string(sc.s[sc.i:]) }

func (sc *scanner) done() bool {
	sc.spaces()
	return sc.i >= len(sc.s)
}

func idRune(r rune) bool {
	return unicode.IsLetter(r) || unicode.IsDigit(r) || r == '_' || r == '.' || r == ':'
}

func (f *flow) parseChain(stmt string) error {
	sc := &scanner{s: []rune(stmt)}
	prev, err := f.parseGroup(sc)
	if err != nil {
		return err
	}
	for !sc.done() {
		e, ok := parseEdge(sc)
		if !ok {
			return fmt.Errorf("can't read %q", ansiCut(sc.rest(), 30))
		}
		next, err := f.parseGroup(sc)
		if err != nil {
			return err
		}
		for _, a := range prev {
			for _, b := range next {
				e.from, e.to = a, b
				f.edges = append(f.edges, e)
			}
		}
		prev = next
	}
	return nil
}

func (f *flow) parseGroup(sc *scanner) ([]int, error) {
	var out []int
	for {
		n, err := f.parseNode(sc)
		if err != nil {
			return nil, err
		}
		out = append(out, n)
		sc.spaces()
		if sc.i < len(sc.s) && sc.s[sc.i] == '&' {
			sc.i++
			continue
		}
		return out, nil
	}
}

func (f *flow) parseNode(sc *scanner) (int, error) {
	sc.spaces()
	start := sc.i
	for sc.i < len(sc.s) {
		r := sc.s[sc.i]
		if idRune(r) || (r == '-' && sc.i+1 < len(sc.s) && (unicode.IsLetter(sc.s[sc.i+1]) || unicode.IsDigit(sc.s[sc.i+1]))) {
			sc.i++
			continue
		}
		break
	}
	id := string(sc.s[start:sc.i])
	if id == "" {
		return 0, fmt.Errorf("expected a node name at %q", ansiCut(sc.rest(), 30))
	}
	label, kind, has := "", square, false
	rest := sc.rest()
	for _, s := range shapes {
		if !strings.HasPrefix(rest, s.open) {
			continue
		}
		end := strings.Index(rest[len(s.open):], s.close)
		if end < 0 {
			return 0, fmt.Errorf("%s%s is missing its closing %s", id, s.open, s.close)
		}
		label = rest[len(s.open) : len(s.open)+end]
		sc.i += len([]rune(rest[:len(s.open)+end+len(s.close)]))
		kind, has = s.shape, true
		break
	}
	n, seen := f.index[id]
	if !seen {
		n = len(f.nodes)
		f.index[id] = n
		f.nodes = append(f.nodes, &node{id: id, lines: []string{id}})
	}
	if has {
		f.nodes[n].lines = labelLines(label)
		f.nodes[n].shape = kind
	}
	return n, nil
}

func labelLines(label string) []string {
	label = strings.Trim(strings.TrimSpace(label), `"`)
	for _, br := range []string{"<br/>", "<br />", "<br>", `\n`} {
		label = strings.ReplaceAll(label, br, "\n")
	}
	var out []string
	for _, line := range strings.Split(label, "\n") {
		out = append(out, ansiCut(strings.TrimSpace(line), 40))
	}
	return out
}

func parseEdge(sc *scanner) (edge, bool) {
	sc.spaces()
	rest := sc.rest()
	if m := edgeText.FindStringSubmatch(rest); m != nil {
		sc.i += len([]rune(m[0]))
		end := m[4]
		return edge{label: edgeLabel(m[3]), dashed: strings.Contains(m[2]+end, "."), tail: m[1] == "<",
			head: strings.HasSuffix(end, ">") || strings.HasSuffix(end, "x") || strings.HasSuffix(end, "o")}, true
	}
	m := edgePlain.FindStringSubmatch(rest)
	if m == nil || m[0] == "" || m[0] == "<" {
		return edge{}, false
	}
	sc.i += len([]rune(m[0]))
	return edge{label: edgeLabel(m[6]), dashed: m[4] != "", tail: m[1] == "<", head: m[3] != "" || m[5] != ""}, true
}

func edgeLabel(label string) string {
	return strings.Join(labelLines(label), " ")
}

func ansiCut(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n-1]) + "…"
}

type item struct {
	node        int
	layer       int
	pos         int
	cross, size int
	main        int
	ups, downs  []*item
}

type route struct {
	items    []*item
	edge     edge
	reversed bool
	channel  int
}

type layout struct {
	f        *flow
	lr       bool
	layers   [][]*item
	nodes    []*item
	routes   []route
	start    []int
	extent   []int
	gap      int
	cross    int
	labelW   int
	backs    int
	crossEnd int
}

func (n *node) box() (int, int) {
	w := 0
	for _, l := range n.lines {
		w = max(w, width(l))
	}
	return w + 4, len(n.lines) + 2
}

func (f *flow) render(dir string) string {
	l := &layout{f: f, lr: dir == "LR"}
	l.rank()
	l.order()
	l.place()
	return l.draw()
}

func (l *layout) rank() {
	f := l.f
	n := len(f.nodes)
	state := make([]int, n)
	back := make([]bool, len(f.edges))
	out := make([][]int, n)
	for i, e := range f.edges {
		out[e.from] = append(out[e.from], i)
	}
	var visit func(int)
	visit = func(v int) {
		state[v] = 1
		for _, ei := range out[v] {
			w := f.edges[ei].to
			switch state[w] {
			case 0:
				visit(w)
			case 1:
				back[ei] = true
			}
		}
		state[v] = 2
	}
	for v := range n {
		if state[v] == 0 {
			visit(v)
		}
	}
	type dag struct {
		a, b int
		ei   int
		rev  bool
	}
	var arcs []dag
	for i, e := range f.edges {
		if e.from == e.to {
			continue
		}
		if back[i] {
			arcs = append(arcs, dag{e.to, e.from, i, true})
		} else {
			arcs = append(arcs, dag{e.from, e.to, i, false})
		}
	}
	layer := make([]int, n)
	for changed := true; changed; {
		changed = false
		for _, a := range arcs {
			if layer[a.b] < layer[a.a]+1 {
				layer[a.b] = layer[a.a] + 1
				changed = true
			}
		}
	}
	top := 0
	for _, v := range layer {
		top = max(top, v)
	}
	l.layers = make([][]*item, top+1)
	l.nodes = make([]*item, n)
	for v := range n {
		it := &item{node: v, layer: layer[v]}
		l.nodes[v] = it
		l.layers[it.layer] = append(l.layers[it.layer], it)
	}
	for _, a := range arcs {
		from, to := l.nodes[a.a], l.nodes[a.b]
		if a.rev {
			l.routes = append(l.routes, route{items: []*item{from, to}, edge: f.edges[a.ei], reversed: true,
				channel: l.backs})
			l.backs++
			continue
		}
		chain := []*item{from}
		for k := from.layer + 1; k < to.layer; k++ {
			d := &item{node: -1, layer: k}
			l.layers[k] = append(l.layers[k], d)
			chain = append(chain, d)
		}
		chain = append(chain, to)
		for i := 1; i < len(chain); i++ {
			chain[i-1].downs = append(chain[i-1].downs, chain[i])
			chain[i].ups = append(chain[i].ups, chain[i-1])
		}
		l.routes = append(l.routes, route{items: chain, edge: f.edges[a.ei], reversed: a.rev})
	}
}

func center(items []*item) float64 {
	if len(items) == 0 {
		return -1
	}
	sum := 0.0
	for _, it := range items {
		sum += float64(it.pos)
	}
	return sum / float64(len(items))
}

func (l *layout) order() {
	for _, layer := range l.layers {
		for i, it := range layer {
			it.pos = i
		}
	}
	sweep := func(k int, neighbours func(*item) []*item) {
		layer := l.layers[k]
		keys := make(map[*item]float64, len(layer))
		for _, it := range layer {
			keys[it] = center(neighbours(it))
			if keys[it] < 0 {
				keys[it] = float64(it.pos)
			}
		}
		sort.SliceStable(layer, func(i, j int) bool { return keys[layer[i]] < keys[layer[j]] })
		for i, it := range layer {
			it.pos = i
		}
	}
	for range 4 {
		for k := 1; k < len(l.layers); k++ {
			sweep(k, func(it *item) []*item { return it.ups })
		}
		for k := len(l.layers) - 2; k >= 0; k-- {
			sweep(k, func(it *item) []*item { return it.downs })
		}
	}
}

func (l *layout) place() {
	for _, r := range l.routes {
		l.labelW = max(l.labelW, width(ansiCut(r.edge.label, maxLabel)))
	}
	l.cross = tdCrossGap
	l.gap = tdGap
	if l.lr {
		l.cross = lrCrossGap
		l.gap = lrMinGap
		if l.labelW > 0 {
			l.gap = max(lrMinGap, 2*l.labelW+4)
		}
	} else if l.labelW > 0 {
		l.cross = max(tdCrossGap, l.labelW+3)
	}
	l.start = make([]int, len(l.layers))
	l.extent = make([]int, len(l.layers))
	at := 0
	for k, layer := range l.layers {
		for _, it := range layer {
			if it.node < 0 {
				it.size = 1
				continue
			}
			w, h := l.f.nodes[it.node].box()
			it.size, it.main = w, h
			if l.lr {
				it.size, it.main = h, w
			}
			l.extent[k] = max(l.extent[k], it.main)
		}
		l.start[k] = at
		at += l.extent[k] + l.gap
	}
	for k, layer := range l.layers {
		end := -l.cross
		for _, it := range layer {
			want := end + l.cross
			if k > 0 && len(it.ups) > 0 {
				mid := 0
				for _, u := range it.ups {
					mid += u.cross + u.size/2
				}
				want = max(want, mid/len(it.ups)-it.size/2)
			}
			it.cross = want
			end = it.cross + it.size
		}
	}
	low := 0
	for _, layer := range l.layers {
		for _, it := range layer {
			low = min(low, it.cross)
		}
	}
	for _, layer := range l.layers {
		for _, it := range layer {
			it.cross -= low
		}
	}
}

func (l *layout) xy(main, cross int) point {
	if l.lr {
		return point{main, cross}
	}
	return point{cross, main}
}

func (l *layout) draw() string {
	crossEnd, mainEnd := 0, 0
	for k, layer := range l.layers {
		for _, it := range layer {
			crossEnd = max(crossEnd, it.cross+it.size)
		}
		mainEnd = l.start[k] + l.extent[k]
	}
	l.crossEnd = crossEnd
	size := l.xy(mainEnd, crossEnd+l.labelW+2*l.backs+3)
	w, h := size.x, size.y
	c := newCanvas(w+1, h+1)
	for _, r := range l.routes {
		l.drawRoute(c, r)
	}
	for _, it := range l.nodes {
		l.drawBox(c, it)
	}
	for _, r := range l.routes {
		l.drawEnds(c, r)
	}
	return c.String()
}

func (l *layout) mid(it *item) int { return it.cross + it.size/2 }

func (l *layout) after(it *item) int {
	if it.node < 0 {
		return l.start[it.layer] + l.extent[it.layer] - 1
	}
	return l.start[it.layer] + it.main - 1
}

func (l *layout) jog(k int) int {
	if l.lr {
		return l.start[k] + l.extent[k] + l.gap/2 - 1
	}
	return l.start[k] + l.extent[k] + 1
}

func (l *layout) points(r route) []point {
	first := r.items[0]
	pts := []point{l.xy(l.after(first)+1, l.mid(first))}
	for i := 1; i < len(r.items); i++ {
		prev, next := r.items[i-1], r.items[i]
		j := l.jog(prev.layer)
		pts = append(pts, l.xy(j, l.mid(prev)), l.xy(j, l.mid(next)))
		if next.node < 0 {
			pts = append(pts, l.xy(l.after(next)+1, l.mid(next)))
		} else {
			pts = append(pts, l.xy(l.start[next.layer]-1, l.mid(next)))
		}
	}
	return pts
}

func (l *layout) drawRoute(c *canvas, r route) {
	if r.reversed {
		c.path(l.backPoints(r), r.edge.dashed)
		return
	}
	c.path(l.points(r), r.edge.dashed)
}

func (l *layout) backPoints(r route) []point {
	target, source := r.items[0], r.items[1]
	lane := l.crossEnd + 1 + 2*r.channel
	from := l.start[source.layer] + source.main/2
	to := l.start[target.layer] + target.main/2
	return []point{l.xy(from, source.cross+source.size), l.xy(from, lane), l.xy(to, lane),
		l.xy(to, target.cross+target.size)}
}

func (l *layout) backEnds(c *canvas, r route) {
	pts := l.backPoints(r)
	start, end := pts[0], pts[len(pts)-1]
	toward, away := '◄', '◄'
	if l.lr {
		toward, away = '▲', '▲'
	}
	if r.edge.head {
		c.put(end.x, end.y, toward)
	}
	source := r.items[1]
	if r.edge.tail {
		c.put(start.x, start.y, away)
	} else if l.f.nodes[source.node].shape != diamond || l.lr {
		attach := l.xy(l.start[source.layer]+source.main/2, source.cross+source.size-1)
		if l.lr {
			c.put(attach.x, attach.y, '┬')
		} else {
			c.put(attach.x, attach.y, '├')
		}
	}
	if r.edge.label != "" {
		at := l.xy((pts[1].y+pts[2].y)/2, pts[1].x+2)
		if l.lr {
			at = point{(pts[1].x + pts[2].x) / 2, pts[1].y + 1}
		}
		c.write(at.x, at.y, ansiCut(r.edge.label, maxLabel))
	}
}

func (l *layout) drawEnds(c *canvas, r route) {
	if r.reversed {
		l.backEnds(c, r)
		return
	}
	pts := l.points(r)
	start, end := pts[0], pts[len(pts)-1]
	headAtEnd, headAtStart := r.edge.head, r.edge.tail
	if r.reversed {
		headAtEnd, headAtStart = r.edge.tail, r.edge.head
	}
	forward, backward := '▼', '▲'
	if l.lr {
		forward, backward = '►', '◄'
	}
	if headAtEnd {
		c.put(end.x, end.y, forward)
	}
	if headAtStart {
		c.put(start.x, start.y, backward)
	} else if l.f.nodes[r.items[0].node].shape != diamond || !l.lr {
		attach := l.xy(l.after(r.items[0]), l.mid(r.items[0]))
		if l.lr {
			c.put(attach.x, attach.y, '├')
		} else {
			c.put(attach.x, attach.y, '┬')
		}
	}
	if r.edge.label == "" {
		return
	}
	label := ansiCut(r.edge.label, maxLabel)
	last := r.items[len(r.items)-1]
	if l.lr {
		before := r.items[len(r.items)-2]
		at := l.xy(l.jog(before.layer)+1, l.mid(last)-1)
		c.write(at.x, at.y, label)
		return
	}
	at := l.xy(l.start[last.layer]-1, l.mid(last)+2)
	c.write(at.x, at.y, label)
}

func (l *layout) drawBox(c *canvas, it *item) {
	n := l.f.nodes[it.node]
	w, h := n.box()
	p := l.xy(l.start[it.layer], it.cross)
	corners := [4]rune{'┌', '┐', '└', '┘'}
	sides := [2]rune{'│', '│'}
	switch n.shape {
	case round:
		corners = [4]rune{'╭', '╮', '╰', '╯'}
	case diamond:
		corners = [4]rune{'╱', '╲', '╲', '╱'}
		sides = [2]rune{'⟨', '⟩'}
	}
	for x := 1; x < w-1; x++ {
		c.put(p.x+x, p.y, '─')
		c.put(p.x+x, p.y+h-1, '─')
	}
	for y := 1; y < h-1; y++ {
		c.put(p.x, p.y+y, sides[0])
		c.put(p.x+w-1, p.y+y, sides[1])
		for x := 1; x < w-1; x++ {
			c.put(p.x+x, p.y+y, ' ')
		}
	}
	c.put(p.x, p.y, corners[0])
	c.put(p.x+w-1, p.y, corners[1])
	c.put(p.x, p.y+h-1, corners[2])
	c.put(p.x+w-1, p.y+h-1, corners[3])
	for i, line := range n.lines {
		pad := (w - 4 - width(line)) / 2
		c.write(p.x+2+pad, p.y+1+i, line)
	}
}
