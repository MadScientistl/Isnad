import { useEffect, useRef, useState } from "react";
import { ApiError, ingestApi, type IngestJob, type JobEvent } from "../api";

export type Step = "source" | "profile" | "proposal" | "review" | "validation" | "approve";
export const STEPS: { key: Step; label: string }[] = [
  { key: "source", label: "Source" }, { key: "profile", label: "Profile" }, { key: "proposal", label: "Proposal" },
  { key: "review", label: "Review" }, { key: "validation", label: "Validation" }, { key: "approve", label: "Approve and import" },
];
export interface WizardState { step: Step; sid?: string; pid?: string; decisionId?: string; comment?: string }

export const errMsg = (e: unknown): string => {
  if (e instanceof ApiError) {
    const code = e.code ? ` (${e.code})` : "";
    if (e.status === 503) return `${e.text}${code}`;
    if (e.status === 413) return `The file is larger than the server accepts. ${e.text}`;
    if (e.status === 429) return `Limit reached: ${e.text}`;
    if (e.status === 404) return "Not found, or this action is for admins only.";
    return `${e.text}${code}`;
  }
  return String((e as Error)?.message ?? e);
};
/** Server events are {seq, kind, payload}; show the payload's message/stage/phase when it has one, else the kind. */
export const eventText = (e: JobEvent | string): string => {
  if (typeof e === "string") return e;
  if (e.message) return String(e.message);
  const pl = (e.payload ?? {}) as Record<string, unknown>;
  const note = ["message", "stage", "phase", "table", "type", "status", "mode"].map((k) => pl[k]).find((v) => typeof v === "string" && v);
  const kind = (e.kind ?? "event").replace(/_/g, " ");
  return note ? `${kind}: ${String(note).slice(0, 120)}` : kind;
};
export const jobErrorText = (e: IngestJob["error"]): string => {
  if (!e) return "The job failed.";
  if (typeof e === "string") return e;
  const d = typeof e.detail === "string" ? e.detail : "The job failed.";
  return e.code ? `${d} (${e.code})` : d;
};

/** Polls GET /ingest/jobs/{id} until the job is done or failed. `onDone` runs once per job. */
export function useJob(jobId: string | null, onDone?: (j: IngestJob) => void) {
  const [job, setJob] = useState<IngestJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const cb = useRef(onDone); cb.current = onDone;
  useEffect(() => {
    setJob(null); setError(null);
    if (!jobId) return;
    let live = true, timer: ReturnType<typeof setTimeout> | undefined, fails = 0;
    const tick = async () => {
      try {
        const j = await ingestApi.job(jobId); if (!live) return;
        fails = 0; setJob(j);
        if (j.status === "done" || j.status === "failed" || j.status === "cancelled") { cb.current?.(j); return; }
      } catch (e) { if (!live) return; if (++fails >= 4) { setError(errMsg(e)); return; } }
      timer = setTimeout(tick, 700);
    };
    tick();
    return () => { live = false; if (timer) clearTimeout(timer); };
  }, [jobId]);
  return { job, error };
}

export function JobLog({ job, error, title }: { job: IngestJob | null; error: string | null; title: string }) {
  const [now, setNow] = useState(0);
  useEffect(() => { const t = setInterval(() => setNow((n) => n + 1), 1000); return () => clearInterval(t); }, []);
  void now;
  const events = job?.events ?? [];
  return (
    <section className="card joblog" aria-label={title} aria-live="polite">
      <h3>{title}</h3>
      {(!job || job.status === "queued" || job.status === "running") && <div className="bar" role="progressbar" aria-label="Working"><i style={{ width: "40%" }} /></div>}
      <ul className="plain small">
        {events.map((e, i) => <li key={i}>{typeof e !== "string" && e.t ? <span className="muted">{String(e.t).slice(11, 19)} </span> : null}{eventText(e)}</li>)}
        {!events.length && !error && <li className="muted">Waiting for the first update…</li>}
      </ul>
      {(job?.status === "failed" || job?.status === "cancelled") && <div className="err" role="alert">{job.status === "cancelled" ? "The job was cancelled." : jobErrorText(job.error)}</div>}
      {error && <div className="err" role="alert">Lost contact with the job: {error}</div>}
    </section>
  );
}

export function Stepper({ step, reached, onGo }: { step: Step; reached: Set<Step>; onGo: (s: Step) => void }) {
  return (
    <ol className="stepper" aria-label="Import steps">
      {STEPS.map((s, i) => {
        const can = reached.has(s.key) && s.key !== step;
        return (
          <li key={s.key} className={`${s.key === step ? "cur" : ""} ${reached.has(s.key) ? "done" : ""}`} aria-current={s.key === step ? "step" : undefined}>
            <button type="button" disabled={!can} onClick={() => onGo(s.key)}><span className="n">{i + 1}</span>{s.label}</button>
          </li>);
      })}
    </ol>
  );
}
