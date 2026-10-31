import { Outlet, useLocation } from "react-router-dom";
import { ErrorBoundary } from "../common/ErrorBoundary";
import { useDashboardSocket } from "../../hooks/useDashboardSocket";
import { Sidebar } from "./Sidebar";

export function Layout() {
  useDashboardSocket();
  const { pathname } = useLocation();
  return (
    <div className="flex h-screen bg-gray-950 text-gray-100 overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        {/* keyed by route so navigating away clears a crashed page */}
        <ErrorBoundary key={pathname} label="This page">
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  );
}
