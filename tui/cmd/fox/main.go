package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"

	"fox/tui/internal/client"
	"fox/tui/internal/config"
	"fox/tui/internal/platform"
	"fox/tui/internal/state"
	"fox/tui/internal/ui/app"
	"fox/tui/internal/ui/theme"
)

var version = "dev"

const (
	defaultServer = "ws://localhost:8765"
	dialTimeout   = 5 * time.Second
)

type repeated []string

func (r *repeated) String() string { return strings.Join(*r, ",") }

func (r *repeated) Set(v string) error {
	*r = append(*r, v)
	return nil
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "fox:", err)
		os.Exit(1)
	}
}

func run() error {
	var attach repeated
	server := flag.String("server", defaultServer, "session server url")
	model := flag.String("model", "", "model key for new sessions (default: any model with a key)")
	permission := flag.String("permission", "", "read-only, workspace-write or full-access (default: server's choice)")
	orchestrator := flag.Bool("orchestrator", false, "start new sessions in orchestrator mode")
	themeName := flag.String("theme", "", "colour theme: "+strings.Join(theme.Names(), " or ")+" (default: saved setting, else "+theme.Default+")")
	flag.Var(&attach, "attach", "attach to a running session (repeatable)")
	flag.Usage = func() {
		fmt.Fprintln(os.Stderr, `usage: fox [flags] ["task"]`)
		flag.PrintDefaults()
	}
	flag.Parse()

	mode := "agent"
	if *orchestrator {
		mode = "orchestrator"
	}

	cfg, err := config.Load()
	if err != nil {
		fmt.Fprintln(os.Stderr, "fox: using default settings:", err)
	}

	if *themeName != "" {
		if _, ok := theme.Palettes[*themeName]; !ok {
			return fmt.Errorf("unknown theme %q, choose %s", *themeName, strings.Join(theme.Names(), " or "))
		}
		cfg.Theme = *themeName
		if err := config.Save(cfg); err != nil {
			fmt.Fprintln(os.Stderr, "fox: couldn't save the theme:", err)
		}
	}

	ctx, cancel := context.WithTimeout(context.Background(), dialTimeout)
	defer cancel()
	c, err := client.Dial(ctx, *server)
	if err != nil {
		return fmt.Errorf("%w (is the server running? from the repo root: py loom\\server.py)", err)
	}
	defer c.Close()

	popout := platform.OpenWindow
	if *server != defaultServer {
		popout = func(args []string) error {
			return platform.OpenWindow(append([]string{"--server", *server}, args...))
		}
	}

	m := app.New(c, state.New(), theme.New(cfg.Theme, cfg.Background), app.Options{
		Version:    version,
		Mode:       mode,
		Model:      *model,
		Permission: *permission,
		Task:       strings.Join(flag.Args(), " "),
		Attach:     attach,
		Popout:     popout,

		SidebarWidth: cfg.SidebarWidth,
		SaveSidebar: func(width int) error {
			cfg.SidebarWidth = width
			return config.Save(cfg)
		},
	})
	p := tea.NewProgram(m)
	go c.Listen(p.Send)
	_, err = p.Run()
	return err
}
