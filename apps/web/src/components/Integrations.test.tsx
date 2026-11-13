import { FluentProvider } from "@fluentui/react-components";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { lightTheme } from "../theme";
import { BacklogSync, IntegrationsForm, type IntegrationsState } from "./Integrations";

const roles = { current: ["project_owner"] };
vi.mock("../auth", () => ({ useAuth: () => ({ rolesIn: () => roles.current }) }));

function renderWith(state: IntegrationsState, ui: React.ReactElement) {
  globalThis.fetch = vi.fn(async () => new Response(JSON.stringify(state), { status: 200 })) as typeof fetch;
  return render(
    <FluentProvider theme={lightTheme}>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        {ui}
      </QueryClientProvider>
    </FluentProvider>,
  );
}

const base: IntegrationsState = {
  config: {},
  connections: [],
  notifiable_events: ["approval.requested", "run.escalated"],
  last_backlog_sync: null,
};

describe("Integrations", () => {
  it("explains that connections are declared by an administrator", async () => {
    renderWith(base, <IntegrationsForm projectId="p1" />);
    expect(
      await screen.findByText(/No work-tool connections are declared on this server/),
    ).toBeInTheDocument();
  });

  it("shows the last backlog sync, including what failed and what waits for a parent", async () => {
    renderWith(
      {
        ...base,
        config: { backlog: { connection: "jira", project_key: "PAY" } },
        connections: [{ name: "jira", kind: "jira", host: "acme.atlassian.net", credential_available: true }],
        last_backlog_sync: {
          connection: "jira",
          created: 3,
          updated: 1,
          unchanged: 2,
          deferred: 2,
          failed: 1,
          failed_items: { "PAY-EPIC": "jira: HTTP 400: issue type is required" },
          at: "2026-11-13T10:00:00Z",
        },
      },
      <BacklogSync projectId="p1" canSync />,
    );
    expect(await screen.findByRole("button", { name: "Sync to Jira" })).toBeInTheDocument();
    expect(
      screen.getByText(/3 created, 1 updated, 2 unchanged, 2 waiting for a parent, 1 failed/),
    ).toBeInTheDocument();
    expect(screen.getByText(/issue type is required/)).toBeInTheDocument();
  });

  it("offers notification events per chat connection", async () => {
    renderWith(
      {
        ...base,
        connections: [{ name: "eng-slack", kind: "slack", host: null, credential_available: true }],
        config: { notifications: [{ connection: "eng-slack", events: ["approval.requested"] }] },
      },
      <IntegrationsForm projectId="p1" />,
    );
    // Each checkbox is labelled by its own event, inside a group named for the connection.
    expect(await screen.findByRole("group", { name: /Notify eng-slack/ })).toBeInTheDocument();
    expect(await screen.findByRole("checkbox", { name: "Approval needed" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Run needs a person" })).not.toBeChecked();
  });
});
