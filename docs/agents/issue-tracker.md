# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the Codex GitHub connector first for Issue and Pull Request operations. Use the `gh` CLI only when the connector does not cover the operation.

## Conventions

- **Create an issue:** use the connector's create issue operation with title, body, and labels.
- **Read an issue:** use the connector's fetch issue operation, including labels and relevant comments when available.
- **Update an issue:** use the connector's update issue operation for title, body, state, labels, assignees, and milestone.
- **Apply or remove labels:** use connector label operations.
- **Create or update a PR:** use connector PR operations after the branch has been pushed.
- **Fallback:** use `D:\AgentDev\gh\bin\gh.exe` for operations the connector does not expose, such as native issue dependencies, Actions logs, or current-branch PR discovery.

The repository is `Ev3rGan/ai-ledger`. When using CLI fallback, run from `D:\AgentDev\ai-agent-dev` and infer the remote from `git remote -v`.

## Pull requests as a triage surface

**PRs as a request surface: no.** Set this to `yes` if the repo later treats external PRs as feature requests; `/triage` reads this flag.

When set to `yes`, PRs run through the same labels and states as issues, using connector PR operations where available:

- **Read a PR:** use connector PR metadata where available; use `gh pr view <number> --comments` and `gh pr diff <number>` only as fallback for missing detail.
- **List external PRs for triage:** use connector PR listing when available; otherwise use `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`, then keep only `authorAssociation` values `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE`.
- **Comment, label, or close:** use connector comment, label, and update operations where available.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either. Resolve it with connector fetch operations first; fall back to `gh pr view 42` and `gh issue view 42` only when needed.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Use the connector's fetch issue operation.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map:** a single issue labelled `wayfinder:map`, holding the Notes, Decisions-so-far, and Fog body. Create it with the connector create issue operation.
- **Child ticket:** an issue linked to the map as a GitHub sub-issue through CLI fallback when connector support is unavailable. Where sub-issues are unavailable, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels use `wayfinder:<type>` (`research`, `prototype`, `grilling`, or `task`). Once claimed, assign the ticket to the driving developer.
- **Blocking:** use GitHub's native issue dependencies. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric database ID from `gh api repos/<owner>/<repo>/issues/<n> --jq .id`. Where dependencies are unavailable, use a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query:** list the map's open children, drop any with an open blocker or assignee, and take the first remaining ticket in map order.
- **Claim:** adding the current agent/developer assignee is the session's first write.
- **Resolve:** comment with the answer, close the ticket, then append a context pointer and link to the map's Decisions-so-far.
