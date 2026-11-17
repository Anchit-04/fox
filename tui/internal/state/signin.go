package state

func (s *Store) NeedsSignIn(sessionID, prefer string) *Task {
	sess := s.Sessions[sessionID]
	if sess == nil {
		return nil
	}
	if t := sess.Tasks[prefer]; t != nil && t.SignIn != "" {
		return t
	}
	for _, id := range sess.TaskOrder {
		if t := sess.Tasks[id]; t != nil && t.SignIn != "" {
			return t
		}
	}
	return nil
}

func (s *Store) SignedIn(model string) {
	for _, sess := range s.Sessions {
		for _, t := range sess.Tasks {
			if t.SignIn == model {
				t.SignIn = ""
			}
		}
	}
}

func (s *Store) LastMessageTo(sessionID, taskID string) string {
	sess := s.Sessions[sessionID]
	if sess == nil {
		return ""
	}
	for i := len(sess.Entries) - 1; i >= 0; i-- {
		if e := sess.Entries[i]; e.Kind == UserEntry && e.TaskID == taskID {
			return e.Text
		}
	}
	return ""
}
