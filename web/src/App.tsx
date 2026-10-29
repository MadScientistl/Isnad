import { useEffect, useState } from "react";
import { AppProvider, useApp } from "./store";
import TimeBar from "./TimeBar";
import ExplorerPage from "./pages/Explorer";
import MapPage from "./pages/MapPage";
import GraphPage from "./pages/GraphPage";
import Dashboard from "./pages/Dashboard";

const ROLES = ["planner", "analyst", "admin"];
const NAV = [["dashboard", "Dashboard"], ["map", "Map"], ["graph", "Graph"], ["", "Explorer"]] as const;

function useParts(): string[] {
  const [h, setH] = useState(location.hash.slice(2));
  useEffect(() => { const f = () => setH(location.hash.slice(2)); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  return h ? h.split("/") : [];
}

function Shell() {
  const { role, setRole } = useApp();
  const parts = useParts();
  const page = parts[0] ?? "";
  const section = ["map", "graph", "dashboard"].includes(page) ? page : "";
  return (
    <div className="shell">
      <header className="top">
        <h1>Amarh</h1>
        <nav className="topnav" aria-label="Pages">
          {NAV.map(([k, l]) => <a key={k} href={`#/${k}`} className={section === k ? "on" : ""} aria-current={section === k ? "page" : undefined}>{l}</a>)}
        </nav>
        <label className="role">Role <select value={role} onChange={(e) => setRole(e.target.value)}>{ROLES.map((r) => <option key={r}>{r}</option>)}</select></label>
      </header>
      <TimeBar />
      <div className="view" key={role}>
        {page === "map" && <MapPage />}
        {page === "graph" && <GraphPage />}
        {page === "dashboard" && <Dashboard />}
        {!section && <ExplorerPage parts={parts} />}
      </div>
    </div>
  );
}

export default function App() { return <AppProvider><Shell /></AppProvider>; }
