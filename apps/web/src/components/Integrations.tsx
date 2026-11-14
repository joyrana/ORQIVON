import {
  Button,
  Checkbox,
  Dropdown,
  Field,
  Input,
  Link,
  MessageBar,
  MessageBarBody,
  Option,
  Radio,
  RadioGroup,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "../api/client";
import { useAuth } from "../auth";
import { ErrorNotice } from "./common";

export type Connection = {
  name: string;
  kind: "jira" | "azure_boards" | "confluence" | "slack" | "teams";
  host: string | null;
  credential_available: boolean;
};
type BacklogBinding = { connection: string; project_key?: string; project?: string; area_path?: string };
type NotificationBinding = { connection: string; events: string[] };
type Environment = "staging" | "production";
export type DeployTarget = {
  name: string;
  kind: "github_actions" | "jenkins" | "azure_pipelines" | "argocd";
  environments: Environment[];
  host: string | null;
  credential_available: boolean;
  health_url: string | null;
};
export type IntegrationsState = {
  config: {
    backlog?: BacklogBinding;
    notifications?: NotificationBinding[];
    deploy?: Partial<Record<Environment, string>>;
  };
  connections: Connection[];
  deploy_targets?: DeployTarget[];
  notifiable_events: string[];
  last_backlog_sync: {
    connection: string;
    created: number;
    updated: number;
    unchanged: number;
    deferred: number;
    failed: number;
    failed_items?: Record<string, string>;
    at: string;
  } | null;
};
export type ExternalLinkT = {
  item_key: string;
  external_key: string | null;
  url: string | null;
  error?: string;
};

const KIND_LABEL: Record<string, string> = {
  jira: "Jira",
  azure_boards: "Azure Boards",
  confluence: "Confluence",
  slack: "Slack",
  teams: "Teams",
  github_actions: "GitHub Actions",
  jenkins: "Jenkins",
  azure_pipelines: "Azure Pipelines",
  argocd: "Argo CD",
};
const EVENT_LABEL: Record<string, string> = {
  "approval.requested": "Approval needed",
  "run.escalated": "Run needs a person",
  "run.failed": "Run failed",
  "run.completed": "Run completed",
  "release.candidate": "Release candidate ready",
  "security.scanned": "Security gate blocked a change",
};

const useStyles = makeStyles({
  section: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM, marginBottom: "32px" },
  row: { display: "flex", gap: tokens.spacingHorizontalM, alignItems: "flex-end", flexWrap: "wrap" },
  muted: { color: tokens.colorNeutralForeground3, fontSize: tokens.fontSizeBase200 },
  events: { display: "flex", flexWrap: "wrap", gap: tokens.spacingHorizontalS },
  fieldset: { border: "none", padding: 0, margin: 0 },
});

export function useIntegrations(projectId: string) {
  return useQuery({
    queryKey: ["project", projectId, "integrations"],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/projects/{project_id}/integrations", {
          params: { path: { project_id: projectId } },
        }),
      ) as Promise<IntegrationsState>,
  });
}

export function useExternalLinks(projectId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["project", projectId, "backlog", "external-links"],
    enabled,
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/projects/{project_id}/backlog/external-links", {
          params: { path: { project_id: projectId } },
        }),
      ) as Promise<ExternalLinkT[]>,
  });
}

/** "Sync to Jira" on the backlog page, with the last result. */
export function BacklogSync({ projectId, canSync }: { projectId: string; canSync: boolean }) {
  const s = useStyles();
  const qc = useQueryClient();
  const q = useIntegrations(projectId);
  const sync = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/projects/{project_id}/backlog/sync", {
          params: { path: { project_id: projectId } },
        }),
      ),
    onSuccess: () => setTimeout(() => void qc.invalidateQueries({ queryKey: ["project", projectId] }), 2000),
  });
  const binding = q.data?.config.backlog;
  if (!binding) return null;
  const conn = q.data?.connections.find((c) => c.name === binding.connection);
  const last = q.data?.last_backlog_sync;
  return (
    <div className={s.row} style={{ marginBottom: 12 }}>
      {canSync && (
        <Button onClick={() => sync.mutate()} disabled={sync.isPending}>
          Sync to {KIND_LABEL[conn?.kind ?? ""] ?? binding.connection}
        </Button>
      )}
      {last && (
        <Text className={s.muted}>
          Last sync {new Date(last.at).toLocaleString()}: {last.created} created, {last.updated} updated,{" "}
          {last.unchanged} unchanged
          {last.deferred ? `, ${last.deferred} waiting for a parent` : ""}
          {last.failed ? `, ${last.failed} failed` : ""}
        </Text>
      )}
      {last?.failed_items &&
        Object.entries(last.failed_items).map(([k, v]) => (
          <MessageBar key={k} intent="warning">
            <MessageBarBody>
              {k}: {v}
            </MessageBarBody>
          </MessageBar>
        ))}
      {sync.error && <ErrorNotice error={sync.error} />}
    </div>
  );
}

/** Import a Confluence page as a source document. */
export function ConfluenceImport({ projectId }: { projectId: string }) {
  const s = useStyles();
  const qc = useQueryClient();
  const q = useIntegrations(projectId);
  const conns = (q.data?.connections ?? []).filter((c) => c.kind === "confluence");
  const [conn, setConn] = useState<string | null>(null);
  const [pageId, setPageId] = useState("");
  const [kind, setKind] = useState<"brd" | "spec" | "transcript">("brd");
  const run = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/projects/{project_id}/documents/import", {
          params: { path: { project_id: projectId } },
          body: { connection: conn ?? conns[0]!.name, page_id: pageId.trim(), kind },
        }),
      ) as Promise<{ status: string; version?: number }>,
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["project", projectId] }),
  });
  if (conns.length === 0) return null;
  return (
    <section className={s.section} aria-label="Import from Confluence">
      <form
        className={s.row}
        onSubmit={(e) => {
          e.preventDefault();
          run.mutate();
        }}
      >
        {conns.length > 1 && (
          <Field label="Confluence">
            <Dropdown
              value={conn ?? conns[0]!.name}
              selectedOptions={[conn ?? conns[0]!.name]}
              onOptionSelect={(_, d) => setConn(d.optionValue ?? null)}
            >
              {conns.map((c) => (
                <Option key={c.name} value={c.name}>
                  {c.name}
                </Option>
              ))}
            </Dropdown>
          </Field>
        )}
        <Field label="Confluence page ID" hint="The number in the page URL">
          <Input value={pageId} onChange={(_, d) => setPageId(d.value)} inputMode="numeric" />
        </Field>
        <Field label="Kind">
          <RadioGroup layout="horizontal" value={kind} onChange={(_, d) => setKind(d.value as typeof kind)}>
            <Radio value="brd" label="Requirements" />
            <Radio value="spec" label="Specification" />
          </RadioGroup>
        </Field>
        <Button type="submit" disabled={!/^\d+$/.test(pageId.trim()) || run.isPending}>
          Import from Confluence
        </Button>
      </form>
      {run.error && <ErrorNotice error={run.error} />}
      {run.data && (
        <MessageBar intent={run.data.status === "unchanged" ? "info" : "success"}>
          <MessageBarBody>
            {run.data.status === "unchanged"
              ? "The page has not changed since the last import."
              : `Imported as version ${run.data.version}. The Business Analyst agent is extracting requirements.`}
          </MessageBarBody>
        </MessageBar>
      )}
    </section>
  );
}

/** Project settings: which declared connections this project uses. */
export function IntegrationsForm({ projectId }: { projectId: string }) {
  const s = useStyles();
  const qc = useQueryClient();
  const { rolesIn } = useAuth();
  const canEdit = rolesIn(projectId).includes("project_owner");
  const q = useIntegrations(projectId);
  const [draft, setDraft] = useState<IntegrationsState["config"] | null>(null);
  const save = useMutation({
    mutationFn: (body: IntegrationsState["config"]) =>
      unwrap(
        api.PUT("/api/v1/projects/{project_id}/integrations", {
          params: { path: { project_id: projectId } },
          body: body as never,
        }),
      ),
    onSuccess: () => {
      setDraft(null);
      void qc.invalidateQueries({ queryKey: ["project", projectId, "integrations"] });
    },
  });
  const test = useMutation({
    mutationFn: (connection: string) =>
      unwrap(
        api.POST("/api/v1/projects/{project_id}/integrations/test-notification", {
          params: { path: { project_id: projectId } },
          body: { connection },
        }),
      ),
  });
  if (!q.data) return q.error ? <ErrorNotice error={q.error} /> : null;
  const cfg = draft ?? q.data.config;
  const trackers = q.data.connections.filter((c) => c.kind === "jira" || c.kind === "azure_boards");
  const chats = q.data.connections.filter((c) => c.kind === "slack" || c.kind === "teams");
  const bound = trackers.find((c) => c.name === cfg.backlog?.connection);
  const targets = q.data.deploy_targets ?? [];
  const setDeploy = (env: Environment, name: string) => {
    const deploy = { ...(cfg.deploy ?? {}) };
    if (name) deploy[env] = name;
    else delete deploy[env];
    setDraft({ ...cfg, deploy });
  };
  const events = (name: string) => cfg.notifications?.find((n) => n.connection === name)?.events ?? [];
  const setEvents = (name: string, list: string[]) =>
    setDraft({
      ...cfg,
      notifications: [
        ...(cfg.notifications ?? []).filter((n) => n.connection !== name),
        ...(list.length ? [{ connection: name, events: list }] : []),
      ],
    });
  return (
    <section className={s.section} aria-labelledby="integrations-h">
      <Text as="h2" id="integrations-h" size={400} weight="semibold">
        Integrations
      </Text>
      {q.data.connections.length === 0 && targets.length === 0 ? (
        <Text>
          No work-tool connections or deployment targets are declared on this server. An administrator adds
          Jira, Azure Boards, Confluence, Slack, Teams, and deployment pipelines in the integrations
          configuration.
        </Text>
      ) : (
        <>
          <div className={s.row}>
            <Field label="Export the backlog to">
              <Dropdown
                disabled={!canEdit}
                value={cfg.backlog?.connection ?? "Not exported"}
                selectedOptions={[cfg.backlog?.connection ?? ""]}
                onOptionSelect={(_, d) =>
                  setDraft({ ...cfg, backlog: d.optionValue ? { connection: d.optionValue } : undefined })
                }
              >
                <Option value="">Not exported</Option>
                {trackers.map((c) => (
                  <Option key={c.name} value={c.name} text={c.name}>
                    {c.name} ({KIND_LABEL[c.kind]}, {c.host})
                    {c.credential_available ? "" : " — no credentials"}
                  </Option>
                ))}
              </Dropdown>
            </Field>
            {bound?.kind === "jira" && (
              <Field label="Jira project key">
                <Input
                  disabled={!canEdit}
                  value={cfg.backlog?.project_key ?? ""}
                  onChange={(_, d) =>
                    setDraft({ ...cfg, backlog: { ...cfg.backlog!, project_key: d.value } })
                  }
                />
              </Field>
            )}
            {bound?.kind === "azure_boards" && (
              <>
                <Field label="Azure DevOps project">
                  <Input
                    disabled={!canEdit}
                    value={cfg.backlog?.project ?? ""}
                    onChange={(_, d) => setDraft({ ...cfg, backlog: { ...cfg.backlog!, project: d.value } })}
                  />
                </Field>
                <Field label="Area path (optional)">
                  <Input
                    disabled={!canEdit}
                    value={cfg.backlog?.area_path ?? ""}
                    onChange={(_, d) =>
                      setDraft({ ...cfg, backlog: { ...cfg.backlog!, area_path: d.value || undefined } })
                    }
                  />
                </Field>
              </>
            )}
          </div>
          {targets.length > 0 && (
            <div className={s.row}>
              {(["staging", "production"] as const).map((env) => {
                const options = targets.filter((t) => t.environments.includes(env));
                if (options.length === 0) return null;
                const label = env === "staging" ? "Deploy staging with" : "Deploy production with";
                const local = env === "staging" ? "Built-in staging area" : "Not deployed";
                return (
                  <Field key={env} label={label}>
                    <Dropdown
                      disabled={!canEdit}
                      value={cfg.deploy?.[env] ?? local}
                      selectedOptions={[cfg.deploy?.[env] ?? ""]}
                      onOptionSelect={(_, d) => setDeploy(env, d.optionValue ?? "")}
                    >
                      <Option value="">{local}</Option>
                      {options.map((t) => (
                        <Option key={t.name} value={t.name} text={t.name}>
                          {t.name} ({KIND_LABEL[t.kind]}, {t.host})
                          {t.credential_available ? "" : " — no credentials"}
                        </Option>
                      ))}
                    </Dropdown>
                  </Field>
                );
              })}
            </div>
          )}
          {chats.map((c) => (
            <fieldset key={c.name} className={s.fieldset}>
              <legend>
                Notify {c.name} ({KIND_LABEL[c.kind]}) when
              </legend>
              <div className={s.events}>
                {q.data.notifiable_events.map((ev) => (
                  <Checkbox
                    key={ev}
                    disabled={!canEdit}
                    label={EVENT_LABEL[ev] ?? ev}
                    checked={events(c.name).includes(ev)}
                    onChange={(_, d) =>
                      setEvents(
                        c.name,
                        d.checked ? [...events(c.name), ev] : events(c.name).filter((x) => x !== ev),
                      )
                    }
                  />
                ))}
                {canEdit && (
                  <Button size="small" onClick={() => test.mutate(c.name)} disabled={test.isPending}>
                    Send a test message
                  </Button>
                )}
              </div>
            </fieldset>
          ))}
          {canEdit && (
            <div>
              <Button onClick={() => save.mutate(cfg)} disabled={save.isPending || draft === null}>
                Save integrations
              </Button>
            </div>
          )}
          {save.error && <ErrorNotice error={save.error} />}
          {test.error && <ErrorNotice error={test.error} />}
          {test.isSuccess && (
            <MessageBar intent="success">
              <MessageBarBody>Test message sent.</MessageBarBody>
            </MessageBar>
          )}
        </>
      )}
    </section>
  );
}

export function ExternalLinkBadge({ link }: { link?: ExternalLinkT }) {
  if (!link?.url) return null;
  return (
    <Link href={link.url} target="_blank" rel="noreferrer" title={link.error ?? "Open in the work tracker"}>
      {link.external_key}
    </Link>
  );
}
