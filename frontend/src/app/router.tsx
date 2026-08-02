import {
  createRootRoute,
  createRoute,
  createRouter,
  lazyRouteComponent,
  Navigate,
  Outlet,
} from "@tanstack/react-router";

const rootRoute = createRootRoute({
  component: Outlet,
  notFoundComponent: () => <Navigate replace to="/configure" />,
});

const indexRoute = createRoute({
  component: () => <Navigate replace to="/configure" />,
  getParentRoute: () => rootRoute,
  path: "/",
});

const ConfigureRoute = lazyRouteComponent(
  () => import("../features/configure/Route"),
  "ConfigureRoute",
);

const configureRoute = createRoute({
  component: ConfigureRoute,
  getParentRoute: () => rootRoute,
  path: "/configure",
});

const encodedConfigureRoute = createRoute({
  component: ConfigureRoute,
  getParentRoute: () => rootRoute,
  path: "/$b64config/configure",
});

const AdminRoute = lazyRouteComponent(() => import("../features/admin/Route"), "AdminRoute");

const adminRoute = createRoute({
  component: AdminRoute,
  getParentRoute: () => rootRoute,
  path: "/admin",
});

const adminIndexRoute = createRoute({
  component: () => <Navigate replace to="/admin/overview" />,
  getParentRoute: () => adminRoute,
  path: "/",
});

const adminPages = [
  {
    component: lazyRouteComponent(() => import("../features/overview/Page"), "OverviewPage"),
    path: "/overview",
  },
  {
    component: lazyRouteComponent(() => import("../features/logs/Page"), "LogsPage"),
    path: "/logs",
  },
  {
    component: lazyRouteComponent(() => import("../features/analytics/Page"), "AnalyticsPage"),
    path: "/analytics",
  },
  {
    component: lazyRouteComponent(() => import("../features/usenet/Page"), "UsenetPage"),
    path: "/usenet",
  },
  {
    component: lazyRouteComponent(() => import("../features/proxy/Page"), "ProxyPage"),
    path: "/proxy",
  },
  {
    component: lazyRouteComponent(() => import("../features/scraping/Page"), "ScrapingPage"),
    path: "/scraping",
  },
  {
    component: lazyRouteComponent(() => import("../features/cometnet/Page"), "CometNetPage"),
    path: "/cometnet",
  },
  {
    component: lazyRouteComponent(() => import("../features/settings/Page"), "SettingsPage"),
    path: "/settings",
  },
  {
    component: lazyRouteComponent(() => import("../features/system/Page"), "SystemPage"),
    path: "/system",
  },
] as const;

const adminPageRoutes = adminPages.map(({ component, path }) =>
  createRoute({
    component,
    getParentRoute: () => adminRoute,
    path,
  }),
);

const routeTree = rootRoute.addChildren([
  indexRoute,
  configureRoute,
  encodedConfigureRoute,
  adminRoute.addChildren([adminIndexRoute, ...adminPageRoutes]),
]);

export const router = createRouter({
  defaultPreload: "intent",
  routeTree,
  scrollRestoration: true,
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
