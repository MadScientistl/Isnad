import type { ReactNode } from "react";

/** Shared small states. All text is rendered as text (never HTML). */
export function Spinner({ label = "Loading" }: { label?: string }) {
  return <span className="spinner" role="status" aria-live="polite"><i aria-hidden="true" />{label}…</span>;
}
export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return <div className="empty" role="status"><b>{title}</b>{children && <p>{children}</p>}{action}</div>;
}
export function ErrorBanner({ error, onRetry, title }: { error?: string | null; onRetry?: () => void; title?: string }) {
  if (!error) return null;
  return <div className="err errbanner" role="alert"><span>{title ? <b>{title}: </b> : null}{error}</span>{onRetry && <button onClick={onRetry}>Retry</button>}</div>;
}
/** Replaces control and bidirectional-override characters in data-derived strings (they can reorder or hide text). */
export const safeText = (s: string) => s.replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]/g, "\u00b7");
/** Truncates long data-derived strings; the full value is in the tooltip. */
export function Trunc({ children, max = 60, className }: { children: unknown; max?: number; className?: string }) {
  const s = safeText(children === null || children === undefined ? "" : typeof children === "string" ? children : typeof children === "object" ? JSON.stringify(children) : String(children));
  return <span className={className} title={s.length > max ? s : undefined}>{s.length > max ? s.slice(0, max - 1) + "…" : s}</span>;
}
export function Bar({ value, max = 1, label, tone }: { value: number; max?: number; label?: string; tone?: "ok" | "warn" | "bad" }) {
  const pct = Math.max(0, Math.min(100, max ? (value / max) * 100 : 0));
  return <span className={`bar ${tone ?? ""}`} role="img" aria-label={label ?? `${Math.round(pct)}%`} title={label ?? `${Math.round(pct)}%`}><i style={{ width: `${pct}%` }} /></span>;
}
export const fmtBytes = (n: number) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MiB` : n >= 1024 ? `${(n / 1024).toFixed(1)} KiB` : `${n} B`);
export const pct = (x: number | null | undefined, d = 1) => (x === null || x === undefined ? "n/a" : `${(x * 100).toFixed(d)}%`);
