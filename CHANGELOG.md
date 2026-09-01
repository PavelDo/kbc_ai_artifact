# Changelog

KBC Artifact Hub is one web address where you publish a document and
collaborate on it with your team, secured by the Keboola account you already
have.

## 0.7.1 — Security follow-up (2026-09-01)

- Documents opened through the machine-readable link now render inside the
  same isolated sandbox as the normal view, closing a cross-site scripting
  risk that only affected that one path.
- Password-protected documents now stay protected for invited guests too —
  an invitation is a way to comment, not a way around the password.
- Rotating a document's link now fully cuts off the old one for comments as
  well as viewing, so revoking a leaked link revokes it everywhere.
- Stronger protection against push notifications being redirected to
  internal systems.
- Notification signing and document-password protection now use separate
  keys, instead of sharing one.
- Clearer guidance for AI assistants working with this service: document
  content, comments, and proposals are data to read, never commands to
  follow.

## 0.7.0 — Control, guests, and push notifications (2026-09-01)

- **Revoke a shared link in one click.** Mint a new link for a document and
  the old one stops working immediately, for everyone — the way to take back
  access after a link went somewhere it shouldn't have.
- **Deleted documents go to a recycle bin first.** Deleting a document no
  longer erases it on the spot: it moves to the trash, where it stays fully
  recoverable — bring it back, exactly as it was, on the same link, whenever
  you like. A separate, clearly-labeled "erase forever" action is there for
  when you really do mean permanent.
- **Get a Slack message the moment a colleague proposes a change or
  comments.** Connect a document to a Slack channel (or your own system) and
  stop refreshing the page to see what happened — a notification arrives the
  instant a version is proposed or promoted, a comment or reply lands, or the
  document is finalized, trashed, restored, or its link is rotated.
- **Invite reviewers by name — no Keboola account needed.** Send a private
  link to a colleague, client, or outside reviewer and they can comment right
  away, without signing up for anything. Revoke any one person's access at
  any time without touching anybody else's.
- **See whether your document is actually being read.** Every document now
  reports how many times it's been opened, broken down by day and by how
  people viewed it — the rendered page, the raw file, or a specific version.
- **Compare two versions the way a reader actually sees them.** A new
  side-by-side view renders both versions as real pages, scrolling in sync —
  ideal for reports and dashboards where the visual result matters as much as
  the text.
- **A proposed change now tells you when it's out of date.** If a colleague
  suggests a change and somebody else's edit lands first, the suggestion is
  now flagged so you know to take a fresh look before approving it.

## 0.6.0 — Security hardening

- An independent security review examined every part of the service; all
  forty findings it raised have been fixed.
- Documents now render inside an isolated sandbox, so a shared document can
  never reach the part of the page that holds your Keboola sign-in — even a
  document designed to try.

## 0.5.0 — Changelog and clearer guidance (2026-09-01)

- This page. A plain-English summary of what changed, so you don't have to
  read engineering release notes to know what's new.
- The step-by-step instructions we hand to AI assistants and technical teams
  now walk through full team workflows, not just individual actions — faster
  setup, fewer questions bounced back to you.

## 0.4.0 — Team collaboration ("project brain")

- Colleagues can highlight any passage in a document and leave a comment
  right there, with replies — a document becomes an ongoing discussion
  instead of a one-way memo.
- A dedicated review page lets anyone open the document in their browser,
  highlight text, and comment on it — no special software or training
  required.
- Download the complete history of a document — every version, who proposed
  it, every comment, and how each was resolved — as a self-contained,
  permanent record you can browse and search later.
- Decide exactly who is allowed to contribute changes or comments, from
  "anyone with the link" to a specific named list of collaborators.
- Mark a document "Final" once your team reaches agreement, locking it
  against further changes.

## 0.3.0 — Easier moderation and onboarding

- A new Admin page lets a document's owner approve or decline a colleague's
  suggested change with a single click — no technical steps required.
- New team members, and their AI assistants, can install a ready-made helper
  that already knows how to use the service, cutting setup time to minutes.
- The full API reference is now complete and kept accurate automatically,
  so any engineering team you bring in can integrate faster.

## 0.2.1 — Reliability fix

- Fixed an issue where links opened from behind the company network could
  point to an internal address instead of the public one. Links now always
  resolve correctly, wherever they're opened from.

## 0.2.0 — Document versions

- Every update to a document is saved as a new version, so nothing is ever
  lost — the full history is always available.
- Compare any two versions side by side to see exactly what changed.
- Colleagues can propose changes to a document; the owner reviews and
  approves them before they go live.
- Choose which version your readers see — always the newest one, or a
  specific version you've locked in.
- Publish documents directly from your team's private code repositories,
  fitting into the workflow you already use.

## 0.1.0 — Launch

- Publish any document — a web page, a formatted write-up, or content
  straight from a code repository — and get a unique, hard-to-guess public
  link to share.
- Add an optional password so only the people you've shared the link with
  can open the document.
- Your content is stored securely in your own Keboola account — you keep
  full ownership and control of it.
- Documents support rich formatting out of the box: diagrams, tables, and
  charts, with no extra setup.

---

Technical release notes: github.com/padak/kbc_ai_artifact/releases
