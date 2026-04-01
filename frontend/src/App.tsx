import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/layout/Layout";
import { Backtesting } from "./pages/Backtesting";
import { Dashboard } from "./pages/Dashboard";
import { Portfolio } from "./pages/Portfolio";
import { Strategies } from "./pages/Strategies";
import { SymbolDetail } from "./pages/SymbolDetail";
import { SystemHealth } from "./pages/SystemHealth";
import { Watchlist } from "./pages/Watchlist";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,       // 30s before background re-fetch
      retry: 2,
      refetchOnWindowFocus: false,
    },
  },
});

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Layout />}>
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="dashboard"   element={<Dashboard />} />
            <Route path="watchlist"   element={<Watchlist />} />
            <Route path="watchlist/:ticker" element={<SymbolDetail />} />
            <Route path="strategies"  element={<Strategies />} />
            <Route path="portfolio"   element={<Portfolio />} />
            <Route path="backtesting" element={<Backtesting />} />
            <Route path="system"      element={<SystemHealth />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

export default App;
