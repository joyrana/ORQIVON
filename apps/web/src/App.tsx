import { lazy, Suspense, useState, type ReactNode } from "react";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import { useAuth } from "./auth";
import { AppShell } from "./components/AppShell";
import { Loading } from "./components/common";
import { ProjectLayout } from "./pages/ProjectLayout";
import { ProjectOverview } from "./pages/ProjectOverview";
import { ProjectsPage } from "./pages/ProjectsPage";
import { SignIn } from "./pages/SignIn";

const ActivityPage = lazy(() => import("./pages/ActivityPage").then((m) => ({ default: m.ActivityPage })));
const ApprovalsPage = lazy(() => import("./pages/ApprovalsPage").then((m) => ({ default: m.ApprovalsPage })));
const ArchitecturePage = lazy(() =>
  import("./pages/ArchitecturePage").then((m) => ({ default: m.ArchitecturePage })),
);
const BacklogPage = lazy(() => import("./pages/BacklogPage").then((m) => ({ default: m.BacklogPage })));
const CapabilitiesPage = lazy(() =>
  import("./pages/CapabilitiesPage").then((m) => ({ default: m.CapabilitiesPage })),
);
const ImplementationPage = lazy(() =>
  import("./pages/ImplementationPage").then((m) => ({ default: m.ImplementationPage })),
);
const QualityPage = lazy(() => import("./pages/QualityPage").then((m) => ({ default: m.QualityPage })));
const ReleasesPage = lazy(() => import("./pages/ReleasesPage").then((m) => ({ default: m.ReleasesPage })));
const RequirementsPage = lazy(() =>
  import("./pages/RequirementsPage").then((m) => ({ default: m.RequirementsPage })),
);
const SourcesPage = lazy(() => import("./pages/SourcesPage").then((m) => ({ default: m.SourcesPage })));
const ProjectSettings = lazy(() =>
  import("./pages/ProjectSettings").then((m) => ({ default: m.ProjectSettings })),
);

const page = (node: ReactNode) => <Suspense fallback={<Loading />}>{node}</Suspense>;

const router = createBrowserRouter([
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <ProjectsPage /> },
      { path: "capabilities", element: page(<CapabilitiesPage />) },
      {
        path: "projects/:projectId",
        element: <ProjectLayout />,
        children: [
          { index: true, element: <ProjectOverview /> },
          { path: "sources", element: page(<SourcesPage />) },
          { path: "requirements", element: page(<RequirementsPage />) },
          { path: "backlog", element: page(<BacklogPage />) },
          { path: "activity", element: page(<ActivityPage />) },
          { path: "architecture", element: page(<ArchitecturePage />) },
          { path: "approvals", element: page(<ApprovalsPage />) },
          { path: "implementation", element: page(<ImplementationPage />) },
          { path: "quality", element: page(<QualityPage />) },
          { path: "releases", element: page(<ReleasesPage />) },
          { path: "settings", element: page(<ProjectSettings />) },
        ],
      },
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
]);

/**
 * Completes single sign-on. The backend redirects to /auth/callback with the session token in the
 * URL fragment (never sent to a server); it is stored, removed from the address bar and history,
 * and the user continues where they started (a same-origin path, enforced by the backend too).
 */
export function consumeSsoCallback(signIn: (token: string) => void): boolean {
  if (window.location.pathname !== "/auth/callback") return false;
  const params = new URLSearchParams(window.location.hash.slice(1));
  const token = params.get("token");
  const back = params.get("return_to") ?? "/";
  const safe = back.startsWith("/") && !back.startsWith("//") ? back : "/";
  window.history.replaceState(null, "", token ? safe : "/");
  if (token) signIn(token);
  return !!token;
}

export function App() {
  const { token, me, loading, signIn } = useAuth();
  const [handled] = useState(() => consumeSsoCallback(signIn));
  if (!token && !handled) return <SignIn />;
  if (loading || !me) return <Loading label="Signing in" />;
  return <RouterProvider router={router} />;
}
