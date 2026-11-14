import {
  Button,
  Field,
  Input,
  MessageBar,
  MessageBarBody,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api, unwrap } from "../api/client";
import { useAuth } from "../auth";

const useStyles = makeStyles({
  page: {
    minHeight: "100vh",
    display: "grid",
    placeItems: "center",
    backgroundColor: tokens.colorNeutralBackground2,
    padding: "16px",
  },
  panel: {
    width: "min(440px, 100%)",
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalL,
    padding: tokens.spacingHorizontalXXL,
    backgroundColor: tokens.colorNeutralBackground1,
    borderRadius: tokens.borderRadiusXLarge,
    boxShadow: tokens.shadow16,
  },
  devList: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalXS },
  devUser: { justifyContent: "flex-start", textAlign: "left" },
});

export function SignIn() {
  const s = useStyles();
  const { signIn } = useAuth();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const info = useQuery({ queryKey: ["system-info"], queryFn: () => unwrap(api.GET("/api/v1/system/info")) });
  const providers = useQuery({
    queryKey: ["sign-in-providers"],
    queryFn: () => unwrap(api.GET("/api/v1/auth/providers")),
  });
  const ssoError = new URLSearchParams(window.location.search).get("sso_error");
  const returnTo = window.location.pathname === "/" ? "/" : window.location.pathname + window.location.search;
  const devUsers = useQuery({
    queryKey: ["dev-users"],
    queryFn: () => unwrap(api.GET("/api/v1/system/dev-users")),
    enabled: !!info.data?.dev_login_enabled,
  });

  async function attempt(t: string) {
    setError(null);
    const res = await fetch("/api/v1/me", { headers: { Authorization: `Bearer ${t}` } });
    if (res.ok) signIn(t);
    else setError("That token was not accepted. Check it and try again.");
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (token.trim()) void attempt(token.trim());
  }

  return (
    <main className={s.page}>
      <form className={s.panel} onSubmit={onSubmit} aria-labelledby="signin-title">
        <Text as="h1" id="signin-title" size={600} weight="semibold">
          Sign in to the workspace
        </Text>
        {ssoError && (
          <MessageBar intent="error">
            <MessageBarBody>Single sign-on did not complete: {ssoError}</MessageBarBody>
          </MessageBar>
        )}
        {providers.data?.providers.map((p) => (
          <Button
            key={p.name}
            as="a"
            appearance="primary"
            href={`${p.login_url}?return_to=${encodeURIComponent(returnTo)}`}
          >
            Sign in with {p.display_name}
          </Button>
        ))}
        {!!providers.data?.providers.length && (
          <Text size={200}>Or use an access token (automation and break-glass accounts):</Text>
        )}
        <Field
          label="Access token"
          validationMessage={error ?? undefined}
          validationState={error ? "error" : "none"}
        >
          <Input type="password" value={token} onChange={(_, d) => setToken(d.value)} autoComplete="off" />
        </Field>
        <Button
          appearance={providers.data?.providers.length ? "secondary" : "primary"}
          type="submit"
          disabled={!token.trim()}
        >
          Sign in
        </Button>
        {info.data?.dev_login_enabled && devUsers.data && (
          <>
            <MessageBar intent="warning">
              <MessageBarBody>
                Development sign-in is enabled. These accounts exist only in {info.data.app_env} and are
                refused in production.
              </MessageBarBody>
            </MessageBar>
            <div className={s.devList}>
              {devUsers.data.map((u) => (
                <Button
                  key={u.email}
                  className={s.devUser}
                  appearance="subtle"
                  onClick={() => void attempt(u.token)}
                >
                  {u.display_name} — {u.roles_hint}
                </Button>
              ))}
            </div>
          </>
        )}
      </form>
    </main>
  );
}
