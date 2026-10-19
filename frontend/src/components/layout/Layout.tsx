import { Outlet } from "react-router-dom";
import { useDashboardSocket } from "../../hooks/useDashboardSocket";
import { Sidebar } from "./Sidebar";

export function Layout() {
  useDashboardSocket();
  return (
    <div className="flex h-screen bg-gray-950 text-gray-100 overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        <Outlet />
      </main>
    </div>
  );
}
