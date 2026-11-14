import {
  Button,
  Field,
  MessageBar,
  MessageBarBody,
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
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "../api/client";
import { useEvidence } from "../api/hooks";
import { useAuth } from "../auth";
import { EmptyState, ErrorNotice, StatusBadge } from "./common";

export type SecurityFinding = {
  scanner: string;
  rule_id: string;
  severity: "critical" | "high" | "medium" | "low";
  title: string;
  path: string;
  line: number | null;
  message: string;
  excerpt: string;
  introduced: boolean;
  fingerprint: string;
  accepted: boolean;
};

export type SecurityScanT = {
  id: string;
  attempt: number;
  base_commit: string;
  head_commit: string;
  status: "passed" | "blocked" | "error";
  scanners: Record<string, { status: string; findings: number; detail?: string }>;
  problems: string[];
  policy: { block_severity: string; block_secrets: boolean };
  blocking: string[];
  findings: SecurityFinding[];
};

const useStyles = makeStyles({
  mono: { fontFamily: tokens.fontFamilyMonospace, fontSize: tokens.fontSizeBase200 },
  muted: { color: tokens.colorNeutralForeground3, fontSize: tokens.fontSizeBase200 },
  summary: {
    display: "flex",
    gap: tokens.spacingHorizontalL,
    flexWrap: "wrap",
    marginBottom: tokens.spacingVerticalM,
  },
  accept: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS, maxWidth: "560px" },
});

/** Security scan results for a run: what the change introduced (and what blocks) first. */
export function SecurityPanel({ projectId, runId }: { projectId: string; runId: string }) {
  const s = useStyles();
  const scans = useEvidence<SecurityScanT[]>(projectId, `security-scans?run_id=${runId}`);
  const { rolesIn } = useAuth();
  const canAccept = rolesIn(projectId).some((r) => r === "project_owner" || r === "release_approver");
  const [accepting, setAccepting] = useState<SecurityFinding | null>(null);
  if (scans.error) return <ErrorNotice error={scans.error} />;
  const latest = scans.data?.[0];
  if (!latest)
    return (
      <EmptyState title="Not scanned yet">
        The Security agent scans the change after the regression suite passes.
      </EmptyState>
    );
  const blocking = new Set(latest.blocking);
  return (
    <>
      <div className={s.summary}>
        <Text>
          <StatusBadge status={latest.status} /> attempt {latest.attempt} ·{" "}
          <span className={s.mono}>
            {latest.base_commit.slice(0, 8)}..{latest.head_commit.slice(0, 8)}
          </span>
        </Text>
        <Text className={s.muted}>
          blocks at {latest.policy.block_severity} and above
          {latest.policy.block_secrets ? "; any new secret blocks" : ""}
        </Text>
        {Object.entries(latest.scanners).map(([name, sc]) => (
          <Text key={name} className={s.muted} title={sc.detail}>
            {name}: {sc.status === "ok" ? `${sc.findings} finding(s)` : sc.status.replace(/_/g, " ")}
          </Text>
        ))}
      </div>
      {latest.problems.map((p) => (
        <MessageBar key={p} intent="error" style={{ marginBottom: 8 }}>
          <MessageBarBody>{p}</MessageBarBody>
        </MessageBar>
      ))}
      {latest.findings.length === 0 ? (
        <EmptyState title="No findings">
          No secrets, risky constructs or vulnerable dependencies were found.
        </EmptyState>
      ) : (
        <Table size="small" aria-label="Security findings">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Finding</TableHeaderCell>
              <TableHeaderCell>Severity</TableHeaderCell>
              <TableHeaderCell>Where</TableHeaderCell>
              <TableHeaderCell>In this change</TableHeaderCell>
              <TableHeaderCell>Decision</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {latest.findings.map((f) => (
              <TableRow key={f.fingerprint}>
                <TableCell>
                  <Text weight="semibold">{f.title}</Text>
                  <div className={s.muted}>{f.message}</div>
                  {f.excerpt && <div className={s.mono}>{f.excerpt}</div>}
                </TableCell>
                <TableCell>{f.severity}</TableCell>
                <TableCell className={s.mono}>
                  {f.path}
                  {f.line ? `:${f.line}` : ""}
                  <div className={s.muted}>
                    {f.scanner} · {f.rule_id}
                  </div>
                </TableCell>
                <TableCell>{f.introduced ? "introduced" : "existing"}</TableCell>
                <TableCell>
                  {f.accepted ? (
                    <StatusBadge status="accepted" />
                  ) : blocking.has(f.fingerprint) ? (
                    <>
                      <StatusBadge status="blocking" />
                      {canAccept && !(f.scanner === "secrets" && f.severity === "critical") && (
                        <Button size="small" appearance="subtle" onClick={() => setAccepting(f)}>
                          Accept risk…
                        </Button>
                      )}
                    </>
                  ) : (
                    <span className={s.muted}>reported</span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {accepting && (
        <AcceptRisk projectId={projectId} finding={accepting} onDone={() => setAccepting(null)} />
      )}
    </>
  );
}

function AcceptRisk({
  projectId,
  finding,
  onDone,
}: {
  projectId: string;
  finding: SecurityFinding;
  onDone: () => void;
}) {
  const s = useStyles();
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const accept = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/projects/{project_id}/security-acceptances", {
          params: { path: { project_id: projectId } },
          body: { fingerprint: finding.fingerprint, reason },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["project", projectId] });
      onDone();
    },
  });
  return (
    <section className={s.accept} aria-label="Accept security risk" style={{ marginTop: 16 }}>
      <Text weight="semibold">Accept “{finding.title}” as a risk for this project</Text>
      <Text className={s.muted}>
        Future scans will not block on this finding. Your name, the reason and the time are recorded in the
        audit log, and the acceptance can be revoked.
      </Text>
      <Field label="Reason (at least 20 characters)" required>
        <Textarea value={reason} onChange={(_, d) => setReason(d.value)} resize="vertical" />
      </Field>
      {accept.error && <ErrorNotice error={accept.error} />}
      <div style={{ display: "flex", gap: 8 }}>
        <Button
          appearance="primary"
          disabled={reason.trim().length < 20 || accept.isPending}
          onClick={() => accept.mutate()}
        >
          Accept risk
        </Button>
        <Button onClick={onDone}>Cancel</Button>
      </div>
    </section>
  );
}
