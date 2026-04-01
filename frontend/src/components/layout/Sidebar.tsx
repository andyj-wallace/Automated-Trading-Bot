import { NavLink } from "react-router-dom";

interface NavItem {
  to: string;
  label: string;
  icon: string;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/dashboard",  label: "Dashboard",     icon: "⬛" },
  { to: "/watchlist",  label: "Watchlist",     icon: "◈" },
  { to: "/strategies", label: "Strategies",    icon: "⬡" },
  { to: "/portfolio",  label: "Portfolio",     icon: "◉" },
  { to: "/backtesting",label: "Backtesting",   icon: "◷" },
  { to: "/system",     label: "System Health", icon: "◎" },
];

export function Sidebar() {
  return (
    <aside className="w-56 shrink-0 h-screen bg-gray-900 border-r border-gray-800 flex flex-col">
      {/* Logo */}
      <div className="px-5 py-5 border-b border-gray-800">
        <p className="text-xs font-semibold text-gray-500 uppercase tracking-widest">
          Trading Bot
        </p>
        <p className="text-lg font-bold text-white leading-tight mt-0.5">
          Control Panel
        </p>
      </div>

      {/* Nav links */}
      <nav className="flex-1 px-2 py-4 space-y-0.5 overflow-y-auto">
        {NAV_ITEMS.map(({ to, label, icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              [
                "flex items-center gap-3 px-3 py-2 rounded-md text-sm font-medium transition-colors",
                isActive
                  ? "bg-blue-600 text-white"
                  : "text-gray-400 hover:bg-gray-800 hover:text-white",
              ].join(" ")
            }
          >
            <span className="text-base leading-none">{icon}</span>
            {label}
          </NavLink>
        ))}
      </nav>

      {/* Footer */}
      <div className="px-5 py-4 border-t border-gray-800">
        <p className="text-xs text-gray-600">v0.1.0 · development</p>
      </div>
    </aside>
  );
}
