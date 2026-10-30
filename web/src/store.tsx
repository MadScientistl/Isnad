import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, ApiError, authApi, getAuth, getBase, HOME_BASE, ingestApi, setAuth, setBase, setRole as applyRole, setScenarioHeader, type AuthSession, type Me, type RoleInfo, type SimState, type TypesInfo, type WorkspaceInfo } from "./api";
import { assignStyles } from "./palette";
import { scnApi, type Scenario } from "./scenarios";

const ls = (k: string, d = "") => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };

/** The pseudo-workspace for the API the app started against (the supply chain demo unless configured otherwise). */
export const DEFAULT_WS = "default";
interface Ctx {
  role: string; setRole: (r: string) => void; me: Me | null; meError: string | null;
  roles: RoleInfo[]; rolesLoaded: boolean; types: TypesInfo | null; typesError: string | null; reloadTypes: () => void;
  auth: AuthSession | null; needLogin: boolean; login: (user: string, password: string) => Promise<void>; logout: () => void;
  workspaces: WorkspaceInfo[] | null; workspace: string; switchWorkspace: (slug: string) => void; reloadWorkspaces: () => void; epoch: number;
  simAvailable: boolean | null;
  asOf: Date | null; setAsOf: (d: Date | null) => void;
  asOfQ: string | undefined; // debounced iso string for queries; undefined = live
  sim: SimState | null; simError: string | null; setSim: (s: SimState) => void;
  scenario: number | null; scenarioInfo: Scenario | null; setScenario: (id: number | null) => void; refreshScenario: () => void;
}
const C = createContext<Ctx | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [auth, setAuthS] = useState<AuthSession | null>(() => getAuth());
  const [needLogin, setNeedLogin] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const [role, setRoleS] = useState(() => auth?.role ?? (ls("role", "admin") === "analyst" ? "planner" : ls("role", "admin")));
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
  const [simAvailable, setSimAvailable] = useState<boolean | null>(null);
  const [roles, setRoles] = useState<RoleInfo[]>([]);
  const [rolesLoaded, setRolesLoaded] = useState(false);
  const [types, setTypes] = useState<TypesInfo | null>(null);
  const [typesError, setTypesError] = useState<string | null>(null);
  const [typesTick, setTypesTick] = useState(0);
  const [workspaces, setWorkspaces] = useState<WorkspaceInfo[] | null>(null);
  const [wsTick, setWsTick] = useState(0);
  const [wsSlug, setWsSlug] = useState(() => ls("isnad.workspace", DEFAULT_WS));
  useEffect(() => { const t = setTimeout(() => setAsOfQ(asOf ? asOf.toISOString() : undefined), 250); return () => clearTimeout(t); }, [asOf]);
  const authTok = auth?.token ?? "";
  useEffect(() => {
    let live = true; setSimAvailable(null);
    let h: ReturnType<typeof setInterval> | undefined;
    const tick = () => api.simState().then((s) => { if (live) { setSim(s); setSimError(null); setSimAvailable(true); } }, (e) => {
      if (!live) return;
      if (e instanceof ApiError && e.status === 404 && e.code === "sim_unavailable") { setSimAvailable(false); setSim(null); setSimError(null); if (h) clearInterval(h); }
      else if (e instanceof ApiError && e.status === 401) { setSimError(null); if (h) clearInterval(h); }
      else setSimError(String(e.message ?? e));
    });
    tick(); h = setInterval(tick, 2000);
    return () => { live = false; if (h) clearInterval(h); };
  }, [role, epoch, authTok]);
  useEffect(() => {
    let live = true; setMe(null); setMeError(null);
    api.me().then((m) => { if (live) { setMe(m); setNeedLogin(false); } }, (e) => {
      if (!live) return;
      if (e instanceof ApiError && e.status === 401) { if (getAuth()) { setAuth(null); setAuthS(null); } setNeedLogin(true); } else setMeError(String(e.message ?? e));
    });
    return () => { live = false; };
  }, [role, epoch, authTok]);
  useEffect(() => {
    let live = true; setRolesLoaded(false);
    api.roles().then((r) => { if (live) { setRoles(r); setRolesLoaded(true); } }, () => { if (live) { setRoles([]); setRolesLoaded(true); } });
    return () => { live = false; };
  }, [epoch, authTok]);
  useEffect(() => {
    let live = true; setTypesError(null);
    api.types().then((t) => live && setTypes(t), (e) => { if (live) { setTypes(null); if (!(e instanceof ApiError && e.status === 401)) setTypesError(String(e.message ?? e)); } });
    return () => { live = false; };
  }, [role, epoch, authTok, typesTick]);
  useEffect(() => {
    let live = true;
    ingestApi.workspaces().then((w) => live && setWorkspaces(w), () => live && setWorkspaces(null));
    return () => { live = false; };
  }, [role, authTok, wsTick]);
  // a stored role that the server does not offer (e.g. a different workspace) falls back to a role it does
  useEffect(() => {
    if (!rolesLoaded || getAuth() || !roles.length || roles.some((r) => r.role === role)) return;
    const next = roles.find((r) => r.role === "admin")?.role ?? roles[0].role;
    applyRole(next); try { localStorage.setItem("role", next); } catch { /* ignore */ }
    setRoleS(next);
  }, [rolesLoaded, roles, role]);
  useEffect(() => { const f = () => { if (getAuth()) { setAuth(null); setAuthS(null); } setNeedLogin(true); }; addEventListener("isnad-unauthorized", f); return () => removeEventListener("isnad-unauthorized", f); }, []);
  const login = useCallback(async (user: string, password: string) => {
    const a = await authApi.login(user, password);
    applyRole(a.role); setRoleS(a.role); setAuthS(a); setNeedLogin(false);
  }, []);
  const logout = useCallback(() => { authApi.logout(); setAuthS(null); setNeedLogin(true); setMe(null); }, []);
  const setScenario = useCallback((id: number | null) => {
    setScenarioHeader(id); try { if (id) localStorage.setItem("scenario", String(id)); else localStorage.removeItem("scenario"); } catch { /* ignore */ }
    setScenarioS(id); if (!id) setScenarioInfo(null);
  }, []);
  // A scenario belongs to one role's view: switching role leaves it.
  const setRole = useCallback((r: string) => { applyRole(r); try { localStorage.setItem("role", r); } catch { /* ignore */ } setRoleS(r); setScenario(null); }, [setScenario]);
  const workspace = getBase() === HOME_BASE ? DEFAULT_WS : (workspaces?.find((w) => w.api_base && w.api_base.replace(/\/$/, "") === getBase())?.slug ?? wsSlug);
  const switchWorkspace = useCallback((slug: string) => {
    const w = slug === DEFAULT_WS ? null : workspaces?.find((x) => x.slug === slug);
    if (slug !== DEFAULT_WS && !w?.api_base) return;
    setBase(w ? w.api_base : null); setWsSlug(slug); try { localStorage.setItem("isnad.workspace", slug); } catch { /* ignore */ }
    setScenario(null); setAsOf(null); setTypes(null); setMe(null); setEpoch((n) => n + 1); location.hash = "#/";
  }, [workspaces, setScenario]);
  const reloadTypes = useCallback(() => setTypesTick((n) => n + 1), []);
  const reloadWorkspaces = useCallback(() => setWsTick((n) => n + 1), []);
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
  const v = useMemo(() => ({ role, setRole, me, meError, roles, rolesLoaded, types, typesError, reloadTypes, auth, needLogin, login, logout, workspaces, workspace, switchWorkspace, reloadWorkspaces, epoch, simAvailable, asOf, setAsOf, asOfQ, sim, simError, setSim, scenario, scenarioInfo, setScenario, refreshScenario }), [role, setRole, me, meError, roles, rolesLoaded, types, typesError, reloadTypes, auth, needLogin, login, logout, workspaces, workspace, switchWorkspace, reloadWorkspaces, epoch, simAvailable, asOf, asOfQ, sim, simError, scenario, scenarioInfo, setScenario, refreshScenario]);
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

/** Per-type colours/shapes derived from the runtime type list (same set of types, same look on every page). */
export function useTypeStyles() {
  const { types } = useApp();
  return useMemo(() => assignStyles((types?.objectTypes ?? []).map((t) => t.name)), [types]);
}
