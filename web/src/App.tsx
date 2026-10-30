import { useEffect, useRef, useState } from "react";
import { AppProvider, useApp, type ToastMsg } from "./store";
import TimeBar from "./TimeBar";
import ExplorerPage from "./pages/Explorer";
import MapPage from "./pages/MapPage";
import GraphPage from "./pages/GraphPage";
import Dashboard from "./pages/Dashboard";
import AuditPage from "./pages/Audit";
import ApprovalsPage from "./pages/Approvals";
import AgentPage from "./pages/Agent";
import ScenariosPage from "./pages/Scenarios";
import ComparePage from "./pages/Compare";
import ScenarioBanner from "./ScenarioBanner";
import Login from "./Login";
import OntologyManager from "./pages/OntologyManager";
import OntologyImport from "./pages/OntologyImport";
import { DEFAULT_WS } from "./store";
import { Spinner } from "./ui";

const NAV = [["dashboard", "Dashboard"], ["map", "Map"], ["graph", "Graph"], ["", "Explorer"], ["audit", "Audit"], ["approvals", "Approvals"], ["agent", "Agent"], ["scenarios", "Scenarios"], ["ontology", "Ontology"]] as const;
const SECTIONS = ["map", "graph", "dashboard", "audit", "approvals", "agent", "scenarios", "ontology"];

function useParts(): string[] {
  const [h, setH] = useState(location.hash.slice(2));
  useEffect(() => { const f = () => setH(location.hash.slice(2)); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  return h ? h.split("/") : [];
}

function Shell() {
  const { role, setRole, me, meError, scenario, roles, rolesLoaded, auth, logout, needLogin, workspaces, workspace, switchWorkspace, epoch } = useApp();
  const parts = useParts();
  const page = parts[0] ?? "";
  const section = SECTIONS.includes(page) ? page : "";
  if (needLogin) return <Login />;
  const wsList = [{ slug: DEFAULT_WS, label: "Supply chain (default)", ok: true }, ...(workspaces ?? []).map((w) => ({ slug: w.slug, label: w.slug, ok: !!w.api_base }))];
  return (
    <div className="shell">
      <header className="top">
        <h1><img src="/logo-mark.svg" alt="" />Isnad</h1>
        <nav className="topnav" aria-label="Pages">
          {NAV.filter(([k]) => k !== "ontology" || role === "admin").map(([k, l]) => <a key={k} href={`#/${k}`} className={section === k ? "on" : ""} aria-current={section === k ? "page" : undefined}>{l}</a>)}
        </nav>
        <div className="role">
          {me && <span className="mesum" title={`user ${me.user}`}>{me.region ? <span className="chip info">region {me.region}</span> : null}
            {Object.entries(me.types).filter(([, t]) => t.masked.length || !t.read).map(([n, t]) => <span key={n} className="chip restricted" title={t.read ? `masked: ${t.masked.join(", ")}` : "no access"}>{n}{t.read ? ` -${t.masked.length}` : " ✕"}</span>)}</span>}
          {meError && <span className="chip bad" title={meError}>/me failed</span>}
          {workspaces && workspaces.length > 0 && (
            <label>Workspace <select value={workspace} onChange={(e) => switchWorkspace(e.target.value)} aria-label="Workspace">
              {wsList.map((w) => <option key={w.slug} value={w.slug} disabled={!w.ok} title={w.ok ? undefined : "No API is running for this workspace"}>{w.label}{w.ok ? "" : " (not running)"}</option>)}</select></label>)}
          {auth ? <span className="mesum"><span title="Signed in">{auth.user}</span><button onClick={logout}>Sign out</button></span>
            : <label>Role <select value={role} onChange={(e) => setRole(e.target.value)} disabled={!rolesLoaded}>
              {!roles.some((r) => r.role === role) && <option value={role}>{role}</option>}
              {roles.map((r) => <option key={r.role} value={r.role} title={r.description || undefined}>{r.role}</option>)}</select></label>}
          <span className="chip badge" aria-label="Active role">{role}</span>
        </div>
      </header>
      <ScenarioBanner />
      {page !== "ontology" && <TimeBar />}
      <div className="view" key={`${role}:${scenario ?? "main"}:${epoch}`}>
        {page === "map" && <MapPage />}
        {page === "graph" && <GraphPage />}
        {page === "dashboard" && <Dashboard />}
        {page === "audit" && <AuditPage parts={parts} />}
        {page === "approvals" && <ApprovalsPage />}
        {page === "agent" && <AgentPage parts={parts} />}
        {page === "scenarios" && (parts[2]?.startsWith("compare") ? <ComparePage /> : <ScenariosPage />)}
        {page === "ontology" && (parts[1] === "import" ? <OntologyImport parts={parts} /> : <OntologyManager />)}
        {!section && <ExplorerPage parts={parts} />}
      </div>
    </div>
  );
}

function Toasts() {
  const [list, setList] = useState<ToastMsg[]>([]);
  useEffect(() => {
    const f = (e: Event) => { const t = (e as CustomEvent<ToastMsg>).detail; setList((l) => [...l, t]); setTimeout(() => setList((l) => l.filter((x) => x.id !== t.id)), 8000); };
    addEventListener("isnad-toast", f); return () => removeEventListener("isnad-toast", f);
  }, []);
  return <div className="toasts" role="status" aria-live="polite">{list.map((t) => <div key={t.id} className={`toast ${t.kind}`}>{t.text}{t.href && <> <a href={t.href}>view</a></>}</div>)}</div>;
}

function Gate() {
  const { needLogin, me, meError } = useApp();
  if (needLogin) return <Login />;
  const seen = useRef(false);
  if (me || meError) seen.current = true;
  if (!seen.current) return <div className="boot"><Spinner label="Connecting" /></div>;
  return <Shell />;
}
export default function App() { return <AppProvider><Gate /><Toasts /></AppProvider>; }
