package layout

type Rect struct {
	X, Y, W, H int
}

func (r Rect) Contains(x, y int) bool {
	return x >= r.X && x < r.X+r.W && y >= r.Y && y < r.Y+r.H
}

func (r Rect) Local(x, y int) (int, int) {
	return x - r.X, y - r.Y
}

func SplitH(r Rect, widths ...int) []Rect {
	sizes := share(r.W, widths)
	out := make([]Rect, len(sizes))
	x := r.X
	for i, w := range sizes {
		out[i] = Rect{X: x, Y: r.Y, W: w, H: r.H}
		x += w
	}
	return out
}

func SplitV(r Rect, heights ...int) []Rect {
	sizes := share(r.H, heights)
	out := make([]Rect, len(sizes))
	y := r.Y
	for i, h := range sizes {
		out[i] = Rect{X: r.X, Y: y, W: r.W, H: h}
		y += h
	}
	return out
}

func share(total int, sizes []int) []int {
	out := make([]int, len(sizes))
	fixed, flex := 0, 0
	for i, s := range sizes {
		if s <= 0 {
			flex++
			continue
		}
		out[i] = s
		fixed += s
	}
	left := max(total-fixed, 0)
	for i, s := range sizes {
		if s > 0 || flex == 0 {
			continue
		}
		out[i] = left / flex
		left -= out[i]
		flex--
	}
	used := 0
	for i := range out {
		out[i] = min(out[i], max(total-used, 0))
		used += out[i]
	}
	return out
}

type Frame struct {
	Sidebar    Rect
	Divider    Rect
	Header     Rect
	Rule       Rect
	Body       Rect
	Composer   Rect
	DropZone   Rect
	Main       Rect
	HasSidebar bool
}

const (
	MinSidebar  = 20
	autoMin     = 24
	autoMax     = 34
	minMain     = 40
	dropZone    = 24
	narrowWidth = 70
)

func SidebarWidth(width, preferred int) int {
	if width < narrowWidth {
		return 0
	}
	if preferred <= 0 {
		preferred = min(max(width/4, autoMin), autoMax)
	}
	return min(max(preferred, MinSidebar), max(width-minMain-1, MinSidebar))
}

func Compute(width, height, sidebarPreferred, composerHeight int) Frame {
	screen := Rect{W: width, H: height}
	var f Frame
	sidebarW := SidebarWidth(width, sidebarPreferred)
	if sidebarW > 0 {
		cols := SplitH(screen, sidebarW, 1, 0)
		f.Sidebar, f.Divider, f.Main = cols[0], cols[1], cols[2]
		f.HasSidebar = true
	} else {
		f.Main = screen
	}
	rows := SplitV(f.Main, 1, 1, 0, composerHeight)
	f.Header, f.Rule, f.Body, f.Composer = rows[0], rows[1], rows[2], rows[3]
	zone := min(dropZone, f.Main.W/2)
	f.DropZone = Rect{X: f.Main.X + f.Main.W - zone, Y: f.Main.Y, W: zone, H: f.Main.H}
	return f
}
