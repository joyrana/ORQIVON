# Work-tool integrations

The workspace works alongside the tools a delivery team already uses. It can:

- **export the backlog** to Jira or Azure Boards and keep it updated;
- **import requirements** from Confluence pages, and from PDF documents as well as Word, Markdown and text;
- **notify** Slack or Microsoft Teams channels when a person is needed.

## Connections

Connections are declared by the operator in `INTEGRATIONS_FILE` (example:
[`infrastructure/config/integrations.example.yaml`](../../infrastructure/config/integrations.example.yaml);
Helm: `config.integrations`). Project owners then choose among the declared connections in
**Project settings → Integrations**. Project users never enter URLs or credentials.

| Kind | Auth | What it needs |
|---|---|---|
| `jira` | `basic` (Cloud: email + API token) or `bearer` (Data Center PAT); `api_version: 3` (Cloud) or `2` (Data Center) | Create and edit issues in the target project |
| `azure_boards` | `pat` | Work Items: Read & write |
| `confluence` | `basic` or `bearer` | Read the pages to import |
| `slack` | incoming webhook URL | — |
| `teams` | Workflows webhook URL (Adaptive Cards) | — |

Rules that hold for every connection:

- Credentials and webhook URLs are read from environment variables named in the declaration.
  They are never stored in the database or returned by the API. The API reports only whether
  each one is present.
- Requests go only to the connection's own host and do not follow redirects. `https` is
  required. Error messages carry the tool's own error text, never our credentials.
- Put these credentials in the worker's secret (`workerSecret` in the Helm chart). The API also
  needs the Confluence and webhook credentials for imports and test messages.

## Backlog export (Jira, Azure Boards)

**Backlog → Sync to Jira** (or Azure Boards) runs in the background and reports created,
updated, unchanged, waiting and failed items.

- **What is sent:** each backlog item becomes one issue or work item: its title (with the
  workspace key), description, acceptance criteria, linked requirement keys, labels, story
  points (Azure Boards; Jira with `story_points_field`), and a link back to the workspace.
- **Idempotent:**
  - Unchanged items are not sent again.
  - A changed item updates the same issue.
  - Nothing is ever duplicated.
  - The tool's workflow status belongs to the team and is never overwritten.
- **Hierarchy:**
  - **Azure Boards** (Agile process): Epic → Feature → User Story, linked as parent and child.
  - **Jira:**
    - Epics sit above stories, so workspace features become epics with their stories under
      them, and the top-level epic is named in the description.
    - Jira Premium hierarchies can be described with `hierarchy_levels` and `issue_types`.
    - Data Center uses issue links (`parent_link_type`).
- **Failures:**
  - Children are never created detached from a parent that failed. They wait, and the next
    sync completes them.
  - Rate limiting (429) is retried once.
  - Other errors are reported per item, with the tool's message.
  - Missing or rejected credentials stop the sync at the first item.

Binding options (`PUT /api/v1/projects/{id}/integrations`):

```json
{
  "backlog": {
    "connection": "jira",
    "project_key": "PAY",
    "issue_types": {"feature": "Epic"},
    "story_points_field": "customfield_10016"
  }
}
```

```json
{"backlog": {"connection": "boards", "project": "Payments", "area_path": "Payments\\Core"}}
```

## Confluence import

**Sources → Import from Confluence** takes a page ID. The page's storage format is converted
to text, so headings, lists and tables (including requirement tables with an ID column)
become located blocks, exactly like a Word upload. Macro parameters are dropped.

- Re-importing a changed page adds a new document version, and requirement re-extraction marks
  what changed.
- Re-importing an unchanged page does nothing.

## PDF documents

Text PDFs are read for headings, paragraphs, lists and page numbers. Every requirement's
locator includes the page.

- **Layout handling:** running headers, footers and page numbers are removed, and hyphenation
  at line ends is undone.
- **What is refused:**
  - encrypted PDFs
  - files with more than 500 pages
  - files that are not PDFs
- **What is ignored:** scripts and attachments inside a PDF; only text is read.
- **Scanned PDFs** have no text layer and are reported as such. There is no OCR, so upload a
  text PDF or the Word original.
- **Tables** in PDFs are read as lines of text. Check extracted rows against the source.

## Notifications (Slack, Teams)

Each chat connection can be subscribed to:

- approval needed
- run needs a person (escalated)
- run failed
- run completed
- release candidate ready
- security gate blocked a change

How delivery works:

- **Content:** a message carries a title, one line and a button that links to the right page.
  No document text, code or findings detail is sent to chat.
- **Transactional:** messages are queued in the same transaction as the event, so a
  rolled-back change never notifies.
- **Retries:** transient failures are retried.
- **Testing:** **Send a test message** checks the webhook from the settings page.
