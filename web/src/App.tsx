import { useEffect, useState } from "react";
import { SELECTABLE_ROLES } from "@sdk/client";
import { AppProvider, useApp, type ToastMsg } from "./store";
import TimeBar from "./TimeBar";
import ExplorerPage from "./pages/Explorer";
import MapPage from "./pages/MapPage";
import GraphPage from "./pages/GraphPage";
import Dashboard from "./pages/Dashboard";
import AuditPage from "./pages/Audit";
import ApprovalsPage from "./pages/Approvals";

const NAV = [["dashboard", "Dashboard"], ["map", "Map"], ["graph", "Graph"], ["", "Explorer"], ["audit", "Audit"], ["approvals", "Approvals"]] as const;

function useParts(): string[] {
  const [h, setH] = useState(location.hash.slice(2));
  useEffect(() => { const f = () => setH(location.hash.slice(2)); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  return h ? h.split("/") : [];
}

function Shell() {
  const { role, setRole, me, meError } = useApp();
  const parts = useParts();
  const page = parts[0] ?? "";
  const section = ["map", "graph", "dashboard", "audit", "approvals"].includes(page) ? page : "";
  return (
    <div className="shell">
      <header className="top">
        <h1>Amarh</h1>
        <nav className="topnav" aria-label="Pages">
          {NAV.map(([k, l]) => <a key={k} href={`#/${k}`} className={section === k ? "on" : ""} aria-current={section === k ? "page" : undefined}>{l}</a>)}
        </nav>
        <div className="role">
          {me && <span className="mesum" title={`user ${me.user}`}>{me.region ? <span className="chip info">region {me.region}</span> : null}
            {Object.entries(me.types).filter(([, t]) => t.masked.length || !t.read).map(([n, t]) => <span key={n} className="chip restricted" title={t.read ? `masked: ${t.masked.join(", ")}` : "no access"}>{n}{t.read ? ` -${t.masked.length}` : " ✕"}</span>)}</span>}
          {meError && <span className="chip bad" title={meError}>/me failed</span>}
          <label>Role <select value={role} onChange={(e) => setRole(e.target.value)}>{SELECTABLE_ROLES.map((r) => <option key={r} value={r}>{r}</option>)}</select></label>
          <span className="chip badge" aria-label="Active role">{role}</span>
        </div>
      </header>
      <TimeBar />
      <div className="view" key={role}>
        {page === "map" && <MapPage />}
        {page === "graph" && <GraphPage />}
        {page === "dashboard" && <Dashboard />}
        {page === "audit" && <AuditPage parts={parts} />}
        {page === "approvals" && <ApprovalsPage />}
        {!section && <ExplorerPage parts={parts} />}
      </div>
    </div>
  );
}

function Toasts() {
  const [list, setList] = useState<ToastMsg[]>([]);
  useEffect(() => {
    const f = (e: Event) => { const t = (e as CustomEvent<ToastMsg>).detail; setList((l) => [...l, t]); setTimeout(() => setList((l) => l.filter((x) => x.id !== t.id)), 8000); };
    addEventListener("amarh-toast", f); return () => removeEventListener("amarh-toast", f);
  }, []);
  return <div className="toasts" role="status" aria-live="polite">{list.map((t) => <div key={t.id} className={`toast ${t.kind}`}>{t.text}{t.href && <> <a href={t.href}>view</a></>}</div>)}</div>;
}

export default function App() { return <AppProvider><Shell /><Toasts /></AppProvider>; }
