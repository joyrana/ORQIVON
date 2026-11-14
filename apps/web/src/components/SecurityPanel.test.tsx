import { FluentProvider } from "@fluentui/react-components";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { lightTheme } from "../theme";
import { SecurityPanel, type SecurityScanT } from "./SecurityPanel";

const roles = { current: ["release_approver"] };
vi.mock("../auth", () => ({ useAuth: () => ({ rolesIn: () => roles.current }) }));

const finding = {
  scanner: "code",
  rule_id: "py-eval",
  severity: "high" as const,
  title: "Dynamic code execution",
  path: "app/main.py",
  line: 4,
  message: "Dynamic code execution (CWE-95).",
  excerpt: "return eval(x)",
  introduced: true,
  accepted: false,
};

const scan: SecurityScanT = {
  id: "s1",
  attempt: 2,
  base_commit: "a".repeat(40),
  head_commit: "b".repeat(40),
  status: "blocked",
  scanners: {
    secrets: { status: "ok", findings: 1 },
    dependencies: { status: "not_configured", findings: 0 },
  },
  problems: [],
  policy: { block_severity: "high", block_secrets: true },
  blocking: ["f1", "f2"],
  findings: [
    { ...finding, fingerprint: "f1" },
    {
      ...finding,
      fingerprint: "f2",
      scanner: "secrets",
      rule_id: "aws-access-key-id",
      severity: "critical",
      title: "AWS access key ID",
      excerpt: "AKIA************Q2",
    },
    { ...finding, fingerprint: "f3", introduced: false, title: "Old eval", line: 9 },
  ],
};

function renderPanel() {
  globalThis.fetch = vi.fn(async () => new Response(JSON.stringify([scan]), { status: 200 })) as typeof fetch;
  return render(
    <FluentProvider theme={lightTheme}>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <SecurityPanel projectId="p1" runId="r1" />
      </QueryClientProvider>
    </FluentProvider>,
  );
}

describe("SecurityPanel", () => {
  it("separates what the change introduced and lets an approver accept only acceptable risks", async () => {
    roles.current = ["release_approver"];
    renderPanel();
    expect(await screen.findByText("Dynamic code execution")).toBeInTheDocument();
    expect(screen.getByText("dependencies: not configured")).toBeInTheDocument();
    expect(screen.getAllByText("introduced")).toHaveLength(2);
    expect(screen.getByText("existing")).toBeInTheDocument();
    // One accept button: a committed critical credential must be removed and rotated, not accepted.
    expect(screen.getAllByRole("button", { name: "Accept risk…" })).toHaveLength(1);
  });

  it("offers no risk acceptance to other roles", async () => {
    roles.current = ["engineer"];
    renderPanel();
    expect(await screen.findByText("Dynamic code execution")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept risk…" })).not.toBeInTheDocument();
  });
});
