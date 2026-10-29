import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { SELECTABLE_ROLES } from "@sdk/client";
import { api, setRole as applyRole, type Me, type SimState } from "./api";

const ls = (k: string, d = "") => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };

interface Ctx {
  role: string; setRole: (r: string) => void; me: Me | null; meError: string | null;
  asOf: Date | null; setAsOf: (d: Date | null) => void;
  asOfQ: string | undefined; // debounced iso string for queries; undefined = live
  sim: SimState | null; simError: string | null; setSim: (s: SimState) => void;
}
const C = createContext<Ctx | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [role, setRoleS] = useState(() => { const r = ls("role", "admin"); return (SELECTABLE_ROLES as string[]).includes(r) ? r : r === "analyst" ? "planner" : "admin"; });
  const [me, setMe] = useState<Me | null>(null);
  const [meError, setMeError] = useState<string | null>(null);
  applyRole(role);
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
  const setRole = useCallback((r: string) => { applyRole(r); try { localStorage.setItem("role", r); } catch { /* ignore */ } setRoleS(r); }, []);
  const v = useMemo(() => ({ role, setRole, me, meError, asOf, setAsOf, asOfQ, sim, simError, setSim }), [role, setRole, me, meError, asOf, asOfQ, sim, simError]);
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
export const toast = (text: string, href?: string, kind: ToastMsg["kind"] = "ok") => window.dispatchEvent(new CustomEvent("amarh-toast", { detail: { id: ++tid, text, href, kind } }));
