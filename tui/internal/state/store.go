package state

import (
	"path/filepath"
	"runtime"
	"strings"

	"fox/tui/internal/protocol"
)

const (
	sidePrefix = "[Side conversation."
	sideMarker = "\nQuestion: "
)

type Status int

const (
	Working Status = iota
	NeedsYou
	Idle
	Done
	Failed
)

type EntryKind int

const (
	UserEntry EntryKind = iota
	AgentEntry
	ToolCallEntry
	ToolResultEntry
	DeniedEntry
	NoticeEntry
	ErrorEntry
)

type Entry struct {
	Kind    EntryKind
	TaskID  string
	Text    string
	Tool    string
	Args    map[string]any
	Pending bool
}

type Task struct {
	ID          string
	Description string
	Lane        string
	Scope       []string
	Status      string
	Model       string
	Error       string
	Report      *protocol.Report
	Cost        float64
}

type Session struct {
	ID             string
	ParentID       string
	Task           string
	Workspace      string
	Mode           string
	PermissionMode string
	Model          string
	Status         Status
	Todos          string
	Entries        []Entry
	Tasks          map[string]*Task
	TaskOrder      []string
	Cost           float64
	CostPartial    bool
	InputTokens    int
	OutputTokens   int
	Unread         bool
	Loaded         bool
	lastSeq        int
}

type Approval struct {
	SessionID string
	TaskID    string
	protocol.ApprovalRequestedPayload
}

type PendingStart struct {
	Task     string
	Mode     string
	ParentID string
}

type Project struct {
	Path     string
	Name     string
	Sessions []*Session
}

type Store struct {
	Sessions  map[string]*Session
	Order     []string
	Approvals []Approval
	Models    []protocol.VaultEntry
	Notices   []string
	pending   []PendingStart
}

func New() *Store {
	return &Store{Sessions: map[string]*Session{}}
}

func (s *Store) ExpectStart(p PendingStart) {
	s.pending = append(s.pending, p)
}

func (s *Store) Session(id string) *Session {
	return s.Sessions[id]
}

func (s *Store) ensure(id string) *Session {
	if sess, ok := s.Sessions[id]; ok {
		return sess
	}
	sess := &Session{ID: id, Tasks: map[string]*Task{}}
	s.Sessions[id] = sess
	s.Order = append(s.Order, id)
	return sess
}

func (s *Store) AddUserMessage(sessionID, taskID, text string) {
	sess := s.Sessions[sessionID]
	if sess == nil {
		return
	}
	sess.Entries = append(sess.Entries, Entry{Kind: UserEntry, TaskID: taskID, Text: text, Pending: true})
	s.setStatus(sess, Working)
}

func (s *Store) Projects() []Project {
	var projects []Project
	index := map[string]int{}
	for _, id := range s.Order {
		sess := s.Sessions[id]
		if sess.ParentID != "" {
			continue
		}
		key := workspaceKey(sess.Workspace)
		i, ok := index[key]
		if !ok {
			name := filepath.Base(sess.Workspace)
			if sess.Workspace == "" {
				name = "this workspace"
			}
			projects = append(projects, Project{Path: sess.Workspace, Name: name})
			i = len(projects) - 1
			index[key] = i
		}
		projects[i].Sessions = append(projects[i].Sessions, sess)
	}
	return projects
}

func workspaceKey(path string) string {
	if path == "" {
		return ""
	}
	path = filepath.Clean(path)
	if runtime.GOOS == "windows" {
		path = strings.ToLower(path)
	}
	return path
}

func (s *Store) Children(parentID string) []*Session {
	var children []*Session
	for _, id := range s.Order {
		if sess := s.Sessions[id]; sess.ParentID == parentID {
			children = append(children, sess)
		}
	}
	return children
}

func (s *Store) ResolveApproval(sessionID, requestID string) (Approval, bool) {
	for i, a := range s.Approvals {
		if a.SessionID == sessionID && a.RequestID == requestID {
			s.Approvals = append(s.Approvals[:i], s.Approvals[i+1:]...)
			s.releaseIfClear(sessionID)
			return a, true
		}
	}
	return Approval{}, false
}

func (s *Store) hasApproval(sessionID string) bool {
	for _, a := range s.Approvals {
		if a.SessionID == sessionID {
			return true
		}
	}
	return false
}

func (s *Store) removeApprovals(drop func(Approval) bool) {
	kept := s.Approvals[:0]
	for _, a := range s.Approvals {
		if !drop(a) {
			kept = append(kept, a)
		}
	}
	s.Approvals = kept
}

func (s *Store) releaseIfClear(sessionID string) {
	if sess := s.Sessions[sessionID]; sess != nil && sess.Status == NeedsYou && !s.hasApproval(sessionID) {
		sess.Status = Working
	}
}

func (s *Store) setStatus(sess *Session, st Status) {
	if st != NeedsYou && s.hasApproval(sess.ID) {
		return
	}
	sess.Status = st
}

func sideQuestion(task string) string {
	if !strings.HasPrefix(task, sidePrefix) {
		return task
	}
	if i := strings.LastIndex(task, sideMarker); i >= 0 {
		return strings.TrimSpace(task[i+len(sideMarker):])
	}
	return task
}

func (sess *Session) add(e Entry) {
	sess.Entries = append(sess.Entries, e)
	sess.Unread = true
}

func (sess *Session) task(id string) *Task {
	if t, ok := sess.Tasks[id]; ok {
		return t
	}
	t := &Task{ID: id}
	sess.Tasks[id] = t
	sess.TaskOrder = append(sess.TaskOrder, id)
	return t
}
