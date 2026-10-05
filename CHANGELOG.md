# Changelog

## 1.4.1 (2026-10-05): the tour on Home

- **Tour on Home**: a "Tour of the screen" card walks the eight steps over the
  1.4 layout picture (docs/assets/layout.svg, now served at /assets/ and bundled
  in the installers), outlining each part in turn. It sits above Recent chats
  until the tour has been taken once, then below. "Show me on the real screen"
  runs the spotlight tour, which gains a first step for the top bar and wording
  for the inspector tabs.

## 1.4.0 (2026-10-05): the redesign

A calmer screen, built from three directions: a Home page, a Workspace, and
the old grid kept as an option.

### Layouts
- **Home** is the first screen unless a conversation is live: continue where you
  left off (with Claude's last words and the plan's progress), start a new chat in
  a folder, recent chats across projects, the setup checklist until it is done,
  usage in a sentence, and the latest outputs.
- **Workspace** is the default once a chat is open: the conversation in the
  centre, the terminal docked below (Hide folds it), and one inspector on the right
  with tabs for Plan, Usage, Output, Files and Config. Ctrl+0 hides and shows it.
- **All panes** keeps the resizable grid, with 1px separators and quiet labels.
- A top bar on every screen: Home, where you are, the ⌘K palette as a search
  field, a 5-hour usage meter, the Layout menu and Resume.

### Conversations
- A quiet session header: project above the title, Export and Context as text
  buttons, Resume in terminal as the one primary action, one line of numbers.
- Everything / Prompts and answers, plus a View menu for thinking, tool calls,
  system messages and agents (remembered). Jump-to chips for errors, agents and
  skills; tool counts moved to the Context panel.
- Each turn folds into one line that says what Claude did ("Read server.py, edited
  2 files, ran 3 commands · 12 steps · 3m 12s") and opens into aligned step rows.
- The sidebar shows turn counts instead of file sizes, a live dot, and a New chat
  button; Add-ons, Theme, Alerts and Quit moved to a bar at its foot.

### Everything else
- The palette opens on suggestions (Resume, New chat, next error, Home) and your
  recent chats.
- The end-of-session card has one main action, Add to session log.
- Words instead of emoji icons; Ember Dark, Ember Light, GitHub Dark and GitHub
  Light lead the theme menu. No hard-coded colours outside the theme tables (tested).
- Server: `GET /api/home`, and a per-session turn count.
