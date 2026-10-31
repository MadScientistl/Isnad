import { useState } from "react";
import { useApp } from "./store";
import { ApiError } from "./api";
import { ErrorBanner } from "./ui";

/** Shown when the server answers 401 (ISNAD_AUTH=1) and no valid token is stored. */
export default function Login() {
  const { login } = useApp();
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true); setErr(undefined);
    try { await login(user.trim(), password); }
    catch (e) { setErr(e instanceof ApiError ? (e.status === 401 ? "Wrong user or password." : e.status === 429 ? "Too many attempts. Wait a minute and try again." : e.text) : String((e as Error).message ?? e)); }
    finally { setBusy(false); setPassword(""); }
  };
  return (
    <main className="loginwrap">
      <form className="card login" onSubmit={(e) => { e.preventDefault(); submit(); }} aria-labelledby="login-h">
        <h2 id="login-h"><img src="/logo-mark.svg" alt="" /> Sign in to Isnad</h2>
        <p className="muted">Every decision, traceable. Your role comes from your account.</p>
        <label>User<input autoFocus autoComplete="username" value={user} onChange={(e) => setUser(e.target.value)} maxLength={80} required /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        <ErrorBanner error={err} />
        <button className="primary" disabled={busy || !user.trim() || !password}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </main>
  );
}
