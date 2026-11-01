package client

import (
	"context"
	"fmt"
	"time"

	tea "charm.land/bubbletea/v2"
	"github.com/coder/websocket"

	"fox/tui/internal/protocol"
)

const (
	sendTimeout = 5 * time.Second
	readLimit   = 64 << 20
)

type EventMsg struct {
	Event protocol.Event
}

type DecodeFailedMsg struct {
	Err error
}

type DisconnectedMsg struct {
	Err error
}

type SendFailedMsg struct {
	Kind string
	Err  error
}

type Client struct {
	conn *websocket.Conn
}

func Dial(ctx context.Context, url string) (*Client, error) {
	conn, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		return nil, fmt.Errorf("dial %s: %w", url, err)
	}
	conn.SetReadLimit(readLimit)
	return &Client{conn: conn}, nil
}

func (c *Client) Close() error {
	return c.conn.Close(websocket.StatusNormalClosure, "")
}

func (c *Client) Send(cmd protocol.Command) error {
	data, err := protocol.Encode(cmd)
	if err != nil {
		return fmt.Errorf("encode %s: %w", cmd.Kind(), err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), sendTimeout)
	defer cancel()
	if err := c.conn.Write(ctx, websocket.MessageText, data); err != nil {
		return fmt.Errorf("send %s: %w", cmd.Kind(), err)
	}
	return nil
}

func (c *Client) SendCmd(cmd protocol.Command) tea.Cmd {
	return func() tea.Msg {
		if err := c.Send(cmd); err != nil {
			return SendFailedMsg{Kind: cmd.Kind(), Err: err}
		}
		return nil
	}
}

func (c *Client) Listen(send func(tea.Msg)) {
	for {
		_, data, err := c.conn.Read(context.Background())
		if err != nil {
			send(DisconnectedMsg{Err: err})
			return
		}
		ev, err := protocol.Decode(data)
		if err != nil {
			send(DecodeFailedMsg{Err: err})
			continue
		}
		send(EventMsg{Event: ev})
	}
}
