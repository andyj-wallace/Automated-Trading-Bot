import { NavLink, useLocation } from "react-router-dom";

interface NavItem {
  to: string;
  label: string;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/dashboard",  label: "Home" },
  { to: "/portfolio",  label: "My Trades" },
  { to: "/strategies", label: "Strategies" },
];

// Tools for tinkering; the bot runs without ever opening these.
const ADVANCED_ITEMS: NavItem[] = [
  { to: "/watchlist",  label: "Watchlist" },
  { to: "/backtesting",label: "Backtesting" },
  { to: "/system",     label: "System Health" },
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
        <p className="text-lg font-bold text-white leading-tight">Trading Bot</p>
      </div>

      {/* Nav links */}
      <nav className="flex-1 px-2 py-4 space-y-0.5 overflow-y-auto">
        {NAV_ITEMS.map(({ to, label }) => (
          <NavLink key={to} to={to} className={linkClass}>
            {label}
          </NavLink>
        ))}

        <details className="pt-4" open={ADVANCED_ITEMS.some((i) => pathname.startsWith(i.to))}>
          <summary className="px-3 py-1 text-xs font-semibold text-gray-400 uppercase tracking-widest cursor-pointer select-none">
            Advanced
          </summary>
          <div className="mt-1 space-y-0.5">
            {ADVANCED_ITEMS.map(({ to, label }) => (
              <NavLink key={to} to={to} className={linkClass}>
                {label}
              </NavLink>
            ))}
          </div>
        </details>
      </nav>
    </aside>
  );
}
