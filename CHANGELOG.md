# Changelog

## 1.5.0 (2026-10-09): what Claude Code users ask for

Eight changes picked from a feature map of the Claude Desktop app and 309 public
issues and comments about Claude Code (quality_reports/replica/).

- **Keep your chats**: Claude Code deletes conversations older than 30 days unless
  `cleanupPeriodDays` says otherwise, and Ember can only show what is still on
  disk. A setup step says so and, with one click, sets it to 10 years: only that
  key changes, after a copy of settings.json is saved (a symlinked settings.json
  is written through, so a dotfiles link survives).
- **Review changes**: the project's working tree in git against HEAD, or against
  where it left any local branch, with new files included. Click a line, or move
  with ↑ ↓ and press Enter, to comment on it; **Send to Claude** types every
  comment, as one line, into the Claude tab (you press Enter). From the session
  header (**Changes**), the end-of-session card and the palette. Read-only: the
  repository's own diff drivers, textconv and fsmonitor are switched off;
  untracked symlinks and secret-looking files are listed but not read, and new
  files stop being read past 2 MB in total (data folders). Names with accents or
  spaces, binary deletions, mode changes and the user's own diff settings keep
  paths and line numbers exact; the repository's own git filters are switched off
  too, git never goes to the network (no lazy fetch) and submodules are summarised,
  not diffed; a git failure (dubious ownership, a 20 s timeout) is shown, not taken
  for "no changes". The session-end card's file count gets the same protections.
- **Needs you**: with the Live activity add-on, sessions waiting on a permission
  prompt appear above the projects with what they ask (read from the transcript;
  the hook still records metadata only), then the ones where it is your turn, for
  an hour. Click to go to the terminal or open the conversation. Background
  subagents and parallel tools no longer flip the state. **Allow once** answers a
  permission prompt only in the Ember tab started with that session and when the
  hook and the transcript name the same call, and it is the only open call of
  that tool; never on a question, a plan approval or a subagent's prompt. Ember
  never types a prompt of its own into a terminal showing a permission prompt.
  The *Live activity* hook now also records `PermissionDenied` (reinstall it from
  Add-ons) so a denied prompt leaves the list.
- **New chat options**: model, effort, permission mode and a git worktree for every
  new chat (Home, the New chat menu, the palette), checked against the CLI's own
  choices. A folder that is not a git repository starts without the worktree, and
  says so; tasks Ember starts for a file (figure feedback, a review) never get one.
  **Fork** (the ▾ next to Resume, or the palette) continues a conversation as a new
  one (`--fork-session`).
- **Account profiles**: Ember lists `~/.claude` and every `~/.claude-<name>` folder
  as an account, switches between them from **Account** in the sidebar, and starts
  terminals with `CLAUDE_CONFIG_DIR` set to the one on screen. **New account
  profile** creates the folder and opens a terminal to sign in. Ember started with
  `CLAUDE_CONFIG_DIR` shows that folder; official limits and the usage baseline
  only come from the profile on screen, and running terminals keep the account
  they started with.
- **Compactions you can see**: a marker above every turn where the context was
  compacted, and a *compactions* jump chip (key **c**). Everything before it stays
  readable.
- **Where the week went**: in the Usage tab, output by who wrote it (your chats, each
  kind of subagent) and an estimate of what each MCP server, skill and tool put into
  the context window over 7 days.
- **Open edits with their diff** (View menu): edit rows open on their own and lead
  with the diff, the raw input folded below. A Write that creates a file shows its
  content as added lines.

## 1.4.3 (2026-10-06): tests

- Removes a Windows unit test for the mark-clearing code that 1.4.2 deleted;
  the release smoke test, which opens the window with the mark in place,
  covers that case. The app itself is unchanged from 1.4.2.

## 1.4.2 (2026-10-06): Windows window from Program Files

- **Windows**: Ember opens its own window again when installed in a folder
  users can't write to, such as `C:\Program Files`. There, the copy unzipped
  from a download kept Windows' "from the internet" mark, and .NET refused
  pythonnet's `Python.Runtime.dll` ("Failed to resolve
  Python.Runtime.Loader.Initialize"), so Ember fell back to the browser. 1.4.1
  removed the mark at launch, which needs write access. Now an `Ember.exe.config`
  next to the exe tells .NET to load the bundle's DLLs anyway, and the release
  smoke test opens the window with the mark still in place.

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
