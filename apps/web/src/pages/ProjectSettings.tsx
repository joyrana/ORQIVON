import {
  Badge,
  Button,
  Dropdown,
  Field,
  Input,
  Option,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Text,
  Textarea,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useOutletContext, useParams } from "react-router-dom";
import { api, unwrap, type Schemas } from "../api/client";
import { useAuth } from "../auth";
import { ErrorNotice, Loading, PageHeader, SampleBadge } from "../components/common";
import { IntegrationsForm } from "../components/Integrations";

/** How a repository's tests will run: the configured or detected profile, or what is missing. */
function TestRunner({ projectId, repositoryId }: { projectId: string; repositoryId: string }) {
  const q = useQuery({
    queryKey: ["project", projectId, "repositories", repositoryId, "test-profile"],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/projects/{project_id}/repositories/{repository_id}/test-profile", {
          params: { path: { project_id: projectId, repository_id: repositoryId } },
        }),
      ),
  });
  if (q.isPending) return <Text size={200}>checking…</Text>;
  if (q.error || !q.data) return <Text size={200}>unknown</Text>;
  if (!q.data.profile) {
    return (
      <Badge color="warning" appearance="tint" title={q.data.problem ?? undefined}>
        not recognised
      </Badge>
    );
  }
  return (
    <span>
      <Text weight="semibold">{String(q.data.profile.name)}</Text> <Text size={200}>({q.data.source})</Text>
    </span>
  );
}

type GitConnection = { name: string; provider: string; host: string; operations: string[] };

const ROLES = ["project_owner", "engineer", "reviewer", "qa", "release_approver", "auditor"];

const useStyles = makeStyles({
  section: {
    marginBottom: tokens.spacingVerticalXXL,
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalM,
  },
  form: { display: "flex", gap: tokens.spacingHorizontalM, alignItems: "flex-end", flexWrap: "wrap" },
  mono: { fontFamily: tokens.fontFamilyMonospace },
  inline: { display: "inline-flex", alignItems: "center", gap: tokens.spacingHorizontalS },
  error: { color: tokens.colorPaletteRedForeground1 },
});

const LIMITS: { key: string; label: string; hint: string }[] = [
  {
    key: "max_repair_attempts",
    label: "Repair attempts",
    hint: "Defect → fix cycles before a person takes over",
  },
  {
    key: "max_architecture_revisions",
    label: "Architecture revisions",
    hint: "Change requests before escalation",
  },
  { key: "max_model_calls", label: "Model calls per run", hint: "Across all agents" },
  { key: "max_tokens", label: "Tokens per run", hint: "Input plus output" },
  { key: "max_cost_usd", label: "Cost per run (USD)", hint: "Self-hosted models report 0" },
  { key: "max_wall_clock_s", label: "Active time per run (s)", hint: "Excludes time waiting for people" },
];

function LimitsForm({ projectId }: { projectId: string }) {
  const s = useStyles();
  const qc = useQueryClient();
  const { project } = useOutletContext<Schemas["ProjectSummary"]>();
  const { rolesIn } = useAuth();
  const [values, setValues] = useState<Record<string, string>>(
    Object.fromEntries(Object.entries(project.limits ?? {}).map(([k, v]) => [k, String(v)])),
  );
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/api/v1/projects/{project_id}", {
          params: { path: { project_id: projectId } },
          body: {
            expected_version: project.version,
            limits: Object.fromEntries(
              Object.entries(values)
                .filter(([, v]) => v.trim() !== "")
                .map(([k, v]) => [k, Number(v)]),
            ),
          },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["project", projectId] }),
  });
  const canEdit = rolesIn(projectId).includes("project_owner");
  return (
    <section className={s.section} aria-labelledby="limits-h">
      <Text as="h2" id="limits-h" size={400} weight="semibold">
        Workflow limits
      </Text>
      <Text>Empty fields use the workspace defaults. Exceeding a limit escalates the run to a person.</Text>
      <form
        className={s.form}
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        {LIMITS.map((l) => (
          <Field key={l.key} label={l.label} hint={l.hint}>
            <Input
              type="number"
              min={0}
              value={values[l.key] ?? ""}
              disabled={!canEdit}
              onChange={(_, d) => setValues((v) => ({ ...v, [l.key]: d.value }))}
            />
          </Field>
        ))}
        {canEdit && (
          <Button type="submit" disabled={save.isPending}>
            Save limits
          </Button>
        )}
      </form>
      {save.error && <ErrorNotice error={save.error} />}
    </section>
  );
}

const SEVERITIES = ["critical", "high", "medium", "low"] as const;

/** The project's security gate: what blocks, and which extra SARIF analysers run in the sandbox. */
function SecurityForm({ projectId }: { projectId: string }) {
  const s = useStyles();
  const qc = useQueryClient();
  const { rolesIn } = useAuth();
  const canEdit = rolesIn(projectId).includes("project_owner");
  const q = useQuery({
    queryKey: ["project", projectId, "security-config"],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/projects/{project_id}/security-config", {
          params: { path: { project_id: projectId } },
        }),
      ),
  });
  const [draft, setDraft] = useState<Schemas["SecurityConfig"] | null>(null);
  const [tools, setTools] = useState<string | null>(null);
  const [toolsError, setToolsError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (body: Schemas["SecurityConfig"]) =>
      unwrap(
        api.PUT("/api/v1/projects/{project_id}/security-config", {
          params: { path: { project_id: projectId } },
          body,
        }),
      ),
    onSuccess: () => {
      setDraft(null);
      setTools(null);
      void qc.invalidateQueries({ queryKey: ["project", projectId, "security-config"] });
    },
  });
  if (q.isPending) return <Loading />;
  if (q.error || !q.data) return <ErrorNotice error={q.error} />;
  const cfg = draft ?? q.data;
  const toolsText = tools ?? JSON.stringify(cfg.tools ?? [], null, 2);
  return (
    <section className={s.section} aria-labelledby="security-h">
      <Text as="h2" id="security-h" size={400} weight="semibold">
        Security gate
      </Text>
      <Text>
        After the regression suite passes, the Security agent scans the change for committed secrets, risky
        code and vulnerable dependencies. Only findings the change introduces can block; existing ones are
        reported.
      </Text>
      <form
        className={s.form}
        onSubmit={(e) => {
          e.preventDefault();
          try {
            const parsed = JSON.parse(toolsText) as Schemas["SecurityConfig"]["tools"];
            setToolsError(null);
            save.mutate({ ...cfg, tools: parsed });
          } catch {
            setToolsError("Analysers must be a JSON list.");
          }
        }}
      >
        <Field label="Block at severity" hint="and above, for findings this change introduces">
          <Dropdown
            value={cfg.block_severity}
            selectedOptions={[cfg.block_severity ?? "high"]}
            disabled={!canEdit}
            onOptionSelect={(_, d) =>
              setDraft({ ...cfg, block_severity: d.optionValue as (typeof SEVERITIES)[number] })
            }
          >
            {SEVERITIES.map((v) => (
              <Option key={v} value={v}>
                {v}
              </Option>
            ))}
          </Dropdown>
        </Field>
        <Switch
          label="Any new secret blocks"
          checked={cfg.block_secrets ?? true}
          disabled={!canEdit}
          onChange={(_, d) => setDraft({ ...cfg, block_secrets: d.checked })}
        />
        <Switch
          label="Require dependency scanning"
          checked={cfg.require_dependency_scan ?? false}
          disabled={!canEdit}
          onChange={(_, d) => setDraft({ ...cfg, require_dependency_scan: d.checked })}
        />
        <Field
          label="SARIF analysers"
          hint='JSON list: [{"name": "semgrep", "command": ["semgrep", "scan", "--sarif", "-o", ".asw/s.sarif"], "sarif": ".asw/s.sarif"}]. They run in the sandbox without network.'
          validationMessage={toolsError ?? undefined}
          style={{ flexBasis: "100%" }}
        >
          <Textarea
            className={s.mono}
            value={toolsText}
            disabled={!canEdit}
            resize="vertical"
            rows={4}
            onChange={(_, d) => setTools(d.value)}
          />
        </Field>
        {canEdit && (
          <Button type="submit" disabled={save.isPending}>
            Save security gate
          </Button>
        )}
      </form>
      {save.error && <ErrorNotice error={save.error} />}
    </section>
  );
}

export function ProjectSettings() {
  const s = useStyles();
  const { projectId = "" } = useParams();
  const { rolesIn } = useAuth();
  const canManage = rolesIn(projectId).includes("project_owner");
  const qc = useQueryClient();
  const path = { params: { path: { project_id: projectId } } };
  const members = useQuery({
    queryKey: ["project", projectId, "members"],
    queryFn: () => unwrap(api.GET("/api/v1/projects/{project_id}/members", path)),
  });
  const repos = useQuery({
    queryKey: ["project", projectId, "repositories"],
    queryFn: () => unwrap(api.GET("/api/v1/projects/{project_id}/repositories", path)),
  });
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("engineer");
  const add = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/v1/projects/{project_id}/members", { ...path, body: { email, role } })),
    onSuccess: () => {
      setEmail("");
      void qc.invalidateQueries({ queryKey: ["project", projectId, "members"] });
    },
  });
  const connections = useQuery({
    queryKey: ["git-connections"],
    queryFn: () => unwrap(api.GET("/api/v1/git-connections")) as Promise<unknown> as Promise<GitConnection[]>,
    enabled: canManage,
  });
  const [remoteName, setRemoteName] = useState("");
  const [remoteUrl, setRemoteUrl] = useState("");
  const [remoteBranch, setRemoteBranch] = useState("main");
  const [remoteConn, setRemoteConn] = useState("");
  const addRemote = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/projects/{project_id}/repositories", {
          ...path,
          body: {
            name: remoteName,
            kind: "git",
            location: remoteUrl,
            default_branch: remoteBranch,
            connection: remoteConn,
          },
        }),
      ),
    onSuccess: () => {
      setRemoteName("");
      setRemoteUrl("");
      void qc.invalidateQueries({ queryKey: ["project", projectId, "repositories"] });
    },
  });
  const syncRepo = useMutation({
    mutationFn: (repositoryId: string) =>
      unwrap(
        api.POST("/api/v1/projects/{project_id}/repositories/{repository_id}/sync", {
          params: { path: { project_id: projectId, repository_id: repositoryId } },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["project", projectId, "repositories"] }),
  });
  const [repoName, setRepoName] = useState("sample-task-service");
  const addRepo = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/projects/{project_id}/repositories", {
          ...path,
          body: { name: repoName, kind: "fixture", location: repoName, default_branch: "main" },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["project", projectId, "repositories"] }),
  });

  return (
    <>
      <PageHeader
        title="Settings"
        description="People, roles, repositories and workflow limits for this project."
      />
      <section className={s.section} aria-labelledby="members-h">
        <Text as="h2" id="members-h" size={400} weight="semibold">
          Members
        </Text>
        {members.isLoading && <Loading />}
        {members.error && <ErrorNotice error={members.error} />}
        {members.data && (
          <Table aria-label="Members" size="small">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Name</TableHeaderCell>
                <TableHeaderCell>Email</TableHeaderCell>
                <TableHeaderCell>Role</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {members.data.map((m) => (
                <TableRow key={`${m.user_id}-${m.role}`}>
                  <TableCell>{m.display_name}</TableCell>
                  <TableCell>{m.email}</TableCell>
                  <TableCell>{m.role.replace(/_/g, " ")}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        {canManage && (
          <form
            className={s.form}
            onSubmit={(e) => {
              e.preventDefault();
              add.mutate();
            }}
          >
            <Field label="Email">
              <Input value={email} onChange={(_, d) => setEmail(d.value)} type="email" />
            </Field>
            <Field label="Role">
              <Dropdown
                value={role.replace(/_/g, " ")}
                selectedOptions={[role]}
                onOptionSelect={(_, d) => setRole(d.optionValue ?? "engineer")}
              >
                {ROLES.map((r) => (
                  <Option key={r} value={r}>
                    {r.replace(/_/g, " ")}
                  </Option>
                ))}
              </Dropdown>
            </Field>
            <Button type="submit" disabled={!email || add.isPending}>
              Add member
            </Button>
          </form>
        )}
        {add.error && <ErrorNotice error={add.error} />}
      </section>
      <section className={s.section} aria-labelledby="repos-h">
        <Text as="h2" id="repos-h" size={400} weight="semibold">
          Repositories
        </Text>
        {repos.data && repos.data.length === 0 && (
          <Text>No repositories yet. Agents need one to analyze and change code.</Text>
        )}
        {repos.data && repos.data.length > 0 && (
          <Table aria-label="Repositories" size="small">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Name</TableHeaderCell>
                <TableHeaderCell>Kind</TableHeaderCell>
                <TableHeaderCell>Default branch</TableHeaderCell>
                <TableHeaderCell>Test runner</TableHeaderCell>
                <TableHeaderCell>Mirror</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {repos.data.map((r) => (
                <TableRow key={r.id}>
                  <TableCell>
                    {r.name} {r.is_sample && <SampleBadge />}
                  </TableCell>
                  <TableCell>
                    {r.kind === "git" ? (
                      <span>
                        <Text>{r.connection}</Text>{" "}
                        <Text size={200} className={s.mono}>
                          {r.location}
                        </Text>
                      </span>
                    ) : (
                      r.kind
                    )}
                  </TableCell>
                  <TableCell>{r.default_branch}</TableCell>
                  <TableCell>
                    <TestRunner projectId={projectId} repositoryId={r.id} />
                  </TableCell>
                  <TableCell>
                    {r.kind !== "git" ? (
                      <Text size={200}>platform-managed sample</Text>
                    ) : (
                      <span className={s.inline}>
                        <Badge
                          appearance="tint"
                          color={
                            (r.sync_status ?? {}).state === "ok"
                              ? "success"
                              : (r.sync_status ?? {}).state === "error"
                                ? "danger"
                                : "warning"
                          }
                          title={String((r.sync_status ?? {}).error ?? "")}
                        >
                          {String((r.sync_status ?? {}).state ?? "pending")}
                        </Badge>
                        {(r.sync_status ?? {}).head ? (
                          <Text size={200} className={s.mono}>
                            {String((r.sync_status ?? {}).head).slice(0, 12)}
                          </Text>
                        ) : null}
                        {canManage && (
                          <Button
                            size="small"
                            appearance="subtle"
                            disabled={syncRepo.isPending}
                            onClick={() => syncRepo.mutate(r.id)}
                          >
                            Sync now
                          </Button>
                        )}
                      </span>
                    )}
                    {(r.sync_status ?? {}).state === "error" && (
                      <Text size={200} block className={s.error}>
                        {String((r.sync_status ?? {}).error)}
                      </Text>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        {canManage && (
          <form
            className={s.form}
            onSubmit={(e) => {
              e.preventDefault();
              addRepo.mutate();
            }}
          >
            <Field
              label="Sample repository"
              hint="Shipped fixtures from samples/repositories. Clearly marked as sample data."
            >
              <Input value={repoName} onChange={(_, d) => setRepoName(d.value)} />
            </Field>
            <Button type="submit" disabled={!repoName || addRepo.isPending}>
              Register sample repository
            </Button>
          </form>
        )}
        {addRepo.error && <ErrorNotice error={addRepo.error} />}
        {canManage && (
          <form
            className={s.form}
            aria-label="Connect a remote repository"
            onSubmit={(e) => {
              e.preventDefault();
              addRemote.mutate();
            }}
          >
            <Field label="Name" required>
              <Input value={remoteName} onChange={(_, d) => setRemoteName(d.value)} />
            </Field>
            <Field label="Clone URL" hint="https only; the host must match the connection" required>
              <Input
                value={remoteUrl}
                placeholder="https://github.com/acme/tasks"
                onChange={(_, d) => setRemoteUrl(d.value)}
              />
            </Field>
            <Field label="Default branch">
              <Input value={remoteBranch} onChange={(_, d) => setRemoteBranch(d.value)} />
            </Field>
            <Field
              label="Connection"
              hint={
                connections.data?.length
                  ? "Configured by an administrator; credentials stay with the worker"
                  : "No git connections are configured for this deployment"
              }
              required
            >
              <Dropdown
                placeholder="Choose a connection"
                selectedOptions={remoteConn ? [remoteConn] : []}
                value={remoteConn}
                onOptionSelect={(_, d) => setRemoteConn(d.optionValue ?? "")}
              >
                {connections.data?.map((c) => (
                  <Option key={c.name} value={c.name} text={c.name}>
                    {`${c.name} (${c.provider}, ${c.host})`}
                  </Option>
                ))}
              </Dropdown>
            </Field>
            <Button type="submit" disabled={!remoteName || !remoteUrl || !remoteConn || addRemote.isPending}>
              Connect repository
            </Button>
          </form>
        )}
        {addRemote.error && <ErrorNotice error={addRemote.error} />}
        {syncRepo.error && <ErrorNotice error={syncRepo.error} />}
      </section>
      <LimitsForm projectId={projectId} />
      <SecurityForm projectId={projectId} />
      <IntegrationsForm projectId={projectId} />
    </>
  );
}
