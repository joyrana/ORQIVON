# Single Sign-On and Provisioning

## Sign-in (OpenID Connect)

Any OIDC provider works: Microsoft Entra ID, Okta, Ping, Keycloak, Google Workspace or Auth0.
Register the workspace as a **web application** (confidential client) with this redirect URI:

```
https://<PUBLIC_BASE_URL>/api/v1/auth/oidc/<provider name>/callback
```

Then declare the provider in the file `IDENTITY_CONFIG_FILE` points to (see
`infrastructure/config/identity.example.yaml`) and put the client secret in the environment
variable it names.

| Setting | Meaning |
|---|---|
| `PUBLIC_BASE_URL` | Where browsers reach the API; https in production |
| `WEB_BASE_URL` | Where the web app is served; https in production |
| `SSO_SESSION_HOURS` | Lifetime of the session a sign-in creates (default 12) |

What the platform checks on every sign-in:

- **Flow:** Authorization Code with PKCE (S256), a single-use state that expires after 10 minutes,
  and a nonce.
- **ID token:**
  - signature against the provider's published keys, refetched when keys rotate
  - only asymmetric algorithms
  - `iss` and `aud`, plus `azp` when there are several audiences
  - `exp`, `iat` and `nbf`, with 60 s of allowed clock skew
- **Email:** it must be verified (`email_verified: true`). `allowed_email_domains` can restrict
  which domains may sign in.
- **Accounts:** a user is identified by provider and subject. The first sign-in never takes over an
  existing account that has the same email, unless the provider sets `link_by_email: true`.
  `auto_provision: false` allows only users who were provisioned in advance.
- **Disabled users** are refused, and so are service accounts.
- **The session token** reaches the browser in the URL fragment, which is never sent to a server
  or written to logs. The page then removes it from the address bar. Signing out revokes the
  session on the server.
- **ID tokens and provider access tokens** are never stored.

API tokens still work for automation and break-glass access.

## Roles from directory groups

`group_roles` maps a group to `admin`, or to a project (by key) and a role. Roles granted this way
are **managed**: they are recomputed whenever the user's groups change. Removing someone from a
group removes the role. Roles a project owner grants by hand are never changed by the sync. Every
change is recorded as a `user.roles_synced` audit event.

Groups come from SCIM when the user is provisioned over SCIM. Otherwise they come from the ID
token's `groups_claim` at each sign-in.

## Provisioning (SCIM 2.0)

Endpoint: `https://<PUBLIC_BASE_URL>/scim/v2`. Authentication: a bearer token. Generate a long
random value, put it in `SCIM_BEARER_TOKEN` on the API, and give the same value to your IdP's
provisioning app. SCIM is off while that variable is unset.

| Supported | Notes |
|---|---|
| `Users`: list (filter `userName`, `externalId`, `emails.value` with `eq`), create, get, replace, patch, delete | Delete deactivates the user and keeps the account, so approvals and audit records keep their author |
| `Groups`: list (filter `displayName`, `externalId`), create, get, replace, patch (add, remove or replace members, rename), delete | Membership drives managed roles |
| `ServiceProviderConfig`, `ResourceTypes` | |
| Not supported | bulk, sort, ETags, password changes |

Deactivating a user (`active: false`, or DELETE) **revokes all of that user's sessions and tokens
immediately**.
