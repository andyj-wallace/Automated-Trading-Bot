import { NavLink, useLocation } from "react-router-dom";

interface NavItem {
  to: string;
  label: string;
  icon: string;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/dashboard",  label: "Home",       icon: "⬛" },
  { to: "/portfolio",  label: "My Trades",  icon: "◉" },
  { to: "/strategies", label: "Strategies", icon: "⬡" },
];

// Tools for tinkering; the bot runs without ever opening these.
const ADVANCED_ITEMS: NavItem[] = [
  { to: "/watchlist",  label: "Watchlist",     icon: "◈" },
  { to: "/backtesting",label: "Backtesting",   icon: "◷" },
  { to: "/system",     label: "System Health", icon: "◎" },
];

function linkClass({ isActive }: { isActive: boolean }) {
  return [
    "flex items-center gap-3 px-3 py-2 rounded-md text-sm font-medium transition-colors",
    isActive ? "bg-blue-600 text-white" : "text-gray-400 hover:bg-gray-800 hover:text-white",
  ].join(" ");
}

export function Sidebar() {
  const { pathname } = useLocation();
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
          <NavLink key={to} to={to} className={linkClass}>
            <span className="text-base leading-none">{icon}</span>
            {label}
          </NavLink>
        ))}

        <details className="pt-4" open={ADVANCED_ITEMS.some((i) => pathname.startsWith(i.to))}>
          <summary className="px-3 py-1 text-xs font-semibold text-gray-600 uppercase tracking-widest cursor-pointer select-none">
            Advanced
          </summary>
          <div className="mt-1 space-y-0.5">
            {ADVANCED_ITEMS.map(({ to, label, icon }) => (
              <NavLink key={to} to={to} className={linkClass}>
                <span className="text-base leading-none">{icon}</span>
                {label}
              </NavLink>
            ))}
          </div>
        </details>
      </nav>

      {/* Footer */}
      <div className="px-5 py-4 border-t border-gray-800">
        <p className="text-xs text-gray-600">v0.1.0 · development</p>
      </div>
    </aside>
  );
}
