import { useCallback, useEffect, useMemo, useState } from "react";
import { ingestApi, type ProposalDetail } from "../api";
import { useApp, usePolled } from "../store";
import { EmptyState, ErrorBanner, Spinner } from "../ui";
import { STEPS, Stepper, type Step, type WizardState } from "../import/common";
import SourceStep from "../import/SourceStep";
import ProfileStep from "../import/ProfileStep";
import ProposalStep from "../import/ProposalStep";
import ReviewStep from "../import/ReviewStep";
import ValidationStep from "../import/ValidationStep";
import ApproveStep from "../import/ApproveStep";

const KEY = "isnad.importWizard";
const load = (): WizardState => { try { const s = JSON.parse(sessionStorage.getItem(KEY) ?? "null"); if (s && STEPS.some((x) => x.key === s.step)) return s; } catch { /* ignore */ } return { step: "source" }; };
const save = (s: WizardState) => { try { sessionStorage.setItem(KEY, JSON.stringify(s)); } catch { /* ignore */ } };
const clear = () => { try { sessionStorage.removeItem(KEY); } catch { /* ignore */ } };

/** Ontology import wizard: Source, Profile, Proposal, Review, Validation, Approve and import. Admin only; state survives a reload. */
export default function OntologyImport({ parts: _parts }: { parts: string[] }) {
  const { role } = useApp();
  const [st, setSt] = useState<WizardState>(load);
  useEffect(() => save(st), [st]);
  const go = useCallback((patch: Partial<WizardState>) => setSt((s) => ({ ...s, ...patch })), []);
  const reached = useMemo(() => {
    const r = new Set<Step>(["source"]);
    if (st.sid) { r.add("profile"); r.add("proposal"); }
    if (st.pid) { r.add("review"); r.add("validation"); }
    if (st.pid && st.step === "approve") r.add("approve");
    return r;
  }, [st]);
  const det = usePolled<ProposalDetail | null>(() => (st.pid ? ingestApi.proposal(st.pid) : Promise.resolve(null)), [st.pid]);
  const sess = usePolled(() => (st.sid ? ingestApi.session(st.sid) : Promise.resolve(null)), [st.sid, st.pid]);
  if (role !== "admin") return <div className="page"><EmptyState title="Ontology import is for admins">Switch to the admin role to import data and propose an ontology.</EmptyState></div>;
  const needDet = st.step === "review" || st.step === "validation" || st.step === "approve";
  const restart = () => { clear(); setSt({ step: "source" }); };
  return (
    <div className="page wizard">
      <h2>Import data as an ontology <a className="lnk small" href="#/ontology">ontology manager</a></h2>
      <p className="muted">Upload tabular data, review a proposed ontology, check it against the data, then approve. Nothing is written to a workspace until you approve.</p>
      <Stepper step={st.step} reached={reached} onGo={(s) => go({ step: s })} />
      {st.sid && <p className="small"><button className="linkbtn" onClick={restart}>Start over with another source</button></p>}
      <div className="stepbody" key={st.step + (st.pid ?? "")}>
        {st.step === "source" && <SourceStep onSession={(sid) => setSt({ step: "profile", sid })} />}
        {st.step === "profile" && st.sid && <ProfileStep sid={st.sid} onNext={() => go({ step: "proposal" })} />}
        {st.step === "proposal" && st.sid && <ProposalStep sid={st.sid} onProposal={(pid) => go({ pid, step: "review", decisionId: undefined })} />}
        {needDet && det.loading && !det.data && <Spinner label="Loading proposal" />}
        {needDet && <ErrorBanner error={det.error} onRetry={det.reload} title="Proposal" />}
        {needDet && !st.pid && <EmptyState title="No proposal selected" action={<button onClick={() => go({ step: "proposal" })}>Choose a proposal</button>}>Generate or open a proposal first.</EmptyState>}
        {needDet && det.data && st.step === "review" && (
          <ReviewStep det={det.data} comment={st.comment ?? ""} setComment={(c) => go({ comment: c })} onApplied={(pid) => go({ pid, decisionId: undefined })} onNext={() => go({ step: "validation" })} />)}
        {needDet && det.data && st.step === "validation" && (
          <ValidationStep det={det.data} rounds={sess.data?.proposals ?? []} onOpen={(pid) => go({ pid })} onRepaired={(pid) => go({ pid, decisionId: undefined })} onNext={() => go({ step: "approve" })} />)}
        {needDet && det.data && st.step === "approve" && (
          <ApproveStep det={det.data} comment={st.comment ?? ""} setComment={(c) => go({ comment: c })} decisionId={st.decisionId} setDecisionId={(d) => go({ decisionId: d })} />)}
      </div>
    </div>
  );
}
