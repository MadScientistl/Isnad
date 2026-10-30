import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { SELECTABLE_ROLES } from "@sdk/client";
import { api, setRole as applyRole, setScenarioHeader, type Me, type SimState } from "./api";
import { scnApi, type Scenario } from "./scenarios";

const ls = (k: string, d = "") => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };

interface Ctx {
  role: string; setRole: (r: string) => void; me: Me | null; meError: string | null;
  asOf: Date | null; setAsOf: (d: Date | null) => void;
  asOfQ: string | undefined; // debounced iso string for queries; undefined = live
  sim: SimState | null; simError: string | null; setSim: (s: SimState) => void;
  scenario: number | null; scenarioInfo: Scenario | null; setScenario: (id: number | null) => void; refreshScenario: () => void;
}
const C = createContext<Ctx | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [role, setRoleS] = useState(() => { const r = ls("role", "admin"); return (SELECTABLE_ROLES as string[]).includes(r) ? r : r === "analyst" ? "planner" : "admin"; });
  const [me, setMe] = useState<Me | null>(null);
  const [meError, setMeError] = useState<string | null>(null);
  applyRole(role);
  const [scenario, setScenarioS] = useState<number | null>(() => { const n = Number(ls("scenario", "")); return Number.isInteger(n) && n > 0 ? n : null; });
  const [scenarioInfo, setScenarioInfo] = useState<Scenario | null>(null);
  setScenarioHeader(scenario);
  const [asOf, setAsOf] = useState<Date | null>(null);
  const [asOfQ, setAsOfQ] = useState<string | undefined>(undefined);
  const [sim, setSim] = useState<SimState | null>(null);
  const [simError, setSimError] = useState<string | null>(null);
  useEffect(() => { const t = setTimeout(() => setAsOfQ(asOf ? asOf.toISOString() : undefined), 250); return () => clearTimeout(t); }, [asOf]);
  useEffect(() => {
    let live = true;
    const tick = () => api.simState().then((s) => { if (live) { setSim(s); setSimError(null); } }, (e) => live && setSimError(String(e.message ?? e)));
    tick(); const h = setInterval(tick, 2000);
    return () => { live = false; clearInterval(h); };
  }, [role]);
  useEffect(() => {
    let live = true; setMe(null); setMeError(null);
    api.me().then((m) => live && setMe(m), (e) => live && setMeError(String(e.message ?? e)));
    return () => { live = false; };
  }, [role]);
  const setScenario = useCallback((id: number | null) => {
    setScenarioHeader(id); try { if (id) localStorage.setItem("scenario", String(id)); else localStorage.removeItem("scenario"); } catch { /* ignore */ }
    setScenarioS(id); if (!id) setScenarioInfo(null);
  }, []);
  // A scenario belongs to one role's view: switching role leaves it.
  const setRole = useCallback((r: string) => { applyRole(r); try { localStorage.setItem("role", r); } catch { /* ignore */ } setRoleS(r); setScenario(null); }, [setScenario]);
  const [scnTick, setScnTick] = useState(0);
  useEffect(() => {
    if (!scenario) { setScenarioInfo(null); return; }
    let live = true;
    const tick = () => scnApi.get(scenario).then((s) => { if (live) { if (s.status !== "open") { setScenario(null); toast(`Scenario "${s.name}" is ${s.status}; back on main`, undefined, "warn"); } else setScenarioInfo(s); } }, (e) => { if (live && /API 404/.test(String(e?.message))) { setScenario(null); toast("The active scenario no longer exists or is not visible to you; back on main", undefined, "warn"); } });
    tick(); const h = setInterval(tick, 4000);
    return () => { live = false; clearInterval(h); };
  }, [scenario, scnTick, setScenario]);
  useEffect(() => {
    const f = (e: Event) => { const id = (e as CustomEvent<number>).detail; if (id === scenario) { setScenario(null); toast("The active scenario no longer exists or is not visible to you; back on main", undefined, "warn"); } };
    addEventListener("isnad-scenario-gone", f); return () => removeEventListener("isnad-scenario-gone", f);
  }, [scenario, setScenario]);
  const refreshScenario = useCallback(() => setScnTick((n) => n + 1), []);
  const v = useMemo(() => ({ role, setRole, me, meError, asOf, setAsOf, asOfQ, sim, simError, setSim, scenario, scenarioInfo, setScenario, refreshScenario }), [role, setRole, me, meError, asOf, asOfQ, sim, simError, scenario, scenarioInfo, setScenario, refreshScenario]);
  return <C.Provider value={v}>{children}</C.Provider>;
}

export function useApp() { const c = useContext(C); if (!c) throw new Error("no provider"); return c; }

/** All pages use this: asOfQ is the debounced iso query value (undefined = live). */
export function useAsOf() {
  const { asOf, setAsOf, asOfQ, sim, role } = useApp();
  return { asOf, setAsOf, asOfQ, live: asOf === null, sim, role };
}

/** Fetch now and whenever deps change; if `pollMs` set, also poll. Keeps previous data while refetching. */
export function usePolled<T>(fn: () => Promise<T>, deps: unknown[], pollMs?: number) {
  const [s, set] = useState<{ data?: T; error?: string; loading: boolean }>({ loading: true });
  const fnRef = useRef(fn); fnRef.current = fn;
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true;
    const run = () => fnRef.current().then((data) => live && set({ data, loading: false }), (e) => live && set((p) => ({ data: p.data, error: String(e.message ?? e), loading: false })));
    set((p) => ({ data: p.data, loading: true })); run();
    const h = pollMs ? setInterval(run, pollMs) : undefined;
    return () => { live = false; if (h) clearInterval(h); };
    // eslint-disable-next-line
  }, [...deps, pollMs, n]);
  return { ...s, reload: () => setN((x) => x + 1) };
}

export const fmtT = (iso: string | Date | null | undefined) => {
  if (!iso) return "";
  const d = typeof iso === "string" ? new Date(iso) : iso;
  return d.toISOString().slice(0, 16).replace("T", " ") + "Z";
};

/** Tiny toast bus: toast(message, href?) shows a transient notice with an optional link. */
export interface ToastMsg { id: number; text: string; href?: string; kind: "ok" | "warn" | "err" }
let tid = 0;
export const toast = (text: string, href?: string, kind: ToastMsg["kind"] = "ok") => window.dispatchEvent(new CustomEvent("isnad-toast", { detail: { id: ++tid, text, href, kind } }));
