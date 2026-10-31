import { useCallback, useRef, useState } from "react";
import { ingestApi, uploadFile } from "../api";
import { usePolled } from "../store";
import { Bar, EmptyState, ErrorBanner, Spinner, fmtBytes } from "../ui";
import { errMsg } from "./common";

export const MAX_UPLOAD = 20 * 1024 * 1024;
const OK_EXT = /\.(csv|tsv|zip|sqlite|db)$/i;

/** Client-side checks mirror the server's (type, size, empty); the server stays the authority. */
export function checkFile(f: File): string | null {
  if (!OK_EXT.test(f.name)) return "Unsupported file type. Use a .csv, .tsv, .zip (of CSV/TSV files) or .sqlite file.";
  if (f.size === 0) return "The file is empty.";
  if (f.size > MAX_UPLOAD) return `The file is ${fmtBytes(f.size)}; the limit is ${fmtBytes(MAX_UPLOAD)}.`;
  return null;
}

export default function SourceStep({ onSession }: { onSession: (sid: string) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [frac, setFrac] = useState(0);
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const sources = usePolled(() => ingestApi.sources(), []);

  const pick = (f: File | undefined | null) => {
    if (!f) return;
    const e = checkFile(f);
    setErr(e ?? undefined); setFile(e ? null : f);
  };
  const upload = useCallback(async (f: File) => {
    setBusy(true); setErr(undefined); setFrac(0);
    abort.current = new AbortController();
    try { const r = await uploadFile(f, setFrac, abort.current.signal); onSession(r.session_id); }
    catch (e) { if ((e as Error)?.name !== "AbortError") setErr(errMsg(e)); }
    finally { setBusy(false); abort.current = null; }
  }, [onSession]);
  const profileSource = async (name: string) => {
    setBusy(true); setErr(undefined);
    try { onSession((await ingestApi.profileSource(name)).session_id); } catch (e) { setErr(errMsg(e)); } finally { setBusy(false); }
  };
  return (
    <div className="grid2">
      <section className="card">
        <h3>Upload a file</h3>
        <div className={`dropzone${drag ? " over" : ""}`} role="button" tabIndex={0} aria-label="Choose or drop a file to upload"
          onClick={() => !busy && input.current?.click()} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), !busy && input.current?.click())}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); if (!busy) pick(e.dataTransfer.files?.[0]); }}>
          <b>Drop a file here, or click to choose</b>
          <span className="muted small">CSV, TSV, ZIP of CSV/TSV, or SQLite. Up to {fmtBytes(MAX_UPLOAD)}.</span>
          <input ref={input} type="file" hidden accept=".csv,.tsv,.zip,.sqlite,.db" onChange={(e) => { pick(e.target.files?.[0]); e.target.value = ""; }} data-testid="file-input" />
        </div>
        {file && !busy && <p className="filerow"><span title={file.name} className="trunc">{file.name}</span> <span className="muted">{fmtBytes(file.size)}</span></p>}
        {busy && file && <div className="uploading" role="status"><Bar value={frac} label={`Uploaded ${Math.round(frac * 100)}%`} /> <span className="small">{Math.round(frac * 100)}% of {fmtBytes(file.size)}</span>
          <button onClick={() => abort.current?.abort()}>Cancel</button></div>}
        <ErrorBanner error={err} onRetry={file && !busy ? () => upload(file) : undefined} />
        <p><button className="primary" disabled={!file || busy} onClick={() => file && upload(file)}>{busy ? "Uploading…" : "Upload and profile"}</button></p>
        <p className="muted small">The file is stored on the server with its checksum. Its contents are only read as data; nothing in it is ever run.</p>
      </section>
      <section className="card">
        <h3>Or use a named SQL source</h3>
        <p className="muted small">Sources are configured on the server (read-only). Connection details are never shown here.</p>
        {sources.loading && !sources.data && <Spinner label="Loading sources" />}
        <ErrorBanner error={sources.error} onRetry={sources.reload} title="Sources" />
        {sources.data && !sources.data.length && <EmptyState title="No named sources configured">An operator can add them with ISNAD_IMPORT_SOURCES on the server.</EmptyState>}
        {sources.data && sources.data.length > 0 && <table><thead><tr><th>Name</th><th>Kind</th><th></th></tr></thead><tbody>
          {sources.data.map((s) => <tr key={s.name}><td><span className="trunc" title={s.name}>{s.name}</span></td><td>{s.kind}</td><td><button disabled={busy} onClick={() => profileSource(s.name)}>Profile</button></td></tr>)}</tbody></table>}
      </section>
    </div>
  );
}
