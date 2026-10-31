import type { ReactNode } from "react";

/** Line icons on a 24px grid, drawn with currentColor (brand kit "Iconography": thin outline, rounded caps). */
const P: Record<string, ReactNode> = {
  dashboard: <><rect x="3" y="3" width="7" height="9" rx="2" /><rect x="14" y="3" width="7" height="5" rx="2" /><rect x="14" y="12" width="7" height="9" rx="2" /><rect x="3" y="16" width="7" height="5" rx="2" /></>,
  map: <><path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2Z" /><path d="M9 4v14M15 6v14" /></>,
  graph: <><circle cx="12" cy="5" r="2.5" /><circle cx="5" cy="19" r="2.5" /><circle cx="19" cy="19" r="2.5" /><path d="m11 7.2-5 9.6M13 7.2l5 9.6M7.5 19h9" /></>,
  explorer: <><ellipse cx="12" cy="6" rx="8" ry="3" /><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></>,
  audit: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  approvals: <><path d="M12 3 4 6v6c0 4.5 3.2 7.8 8 9 4.8-1.2 8-4.5 8-9V6l-8-3Z" /><path d="m8.5 12 2.5 2.5 4.5-5" /></>,
  agent: <><path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3Z" /><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15Z" /></>,
  scenarios: <><path d="M6 3v6a4 4 0 0 0 4 4h4a4 4 0 0 1 4 4v4" /><circle cx="6" cy="3" r="1.5" /><circle cx="18" cy="21" r="1.5" /><path d="M6 13v8" /><circle cx="6" cy="21" r="1.5" /></>,
  ontology: <><rect x="3" y="3" width="7" height="7" rx="2" /><rect x="14" y="14" width="7" height="7" rx="2" /><path d="M10 6.5h4a3 3 0 0 1 3 3V14" /></>,
  send: <><path d="M12 19V5M5 12l7-7 7 7" /></>,
  close: <><path d="M6 6l12 12M18 6 6 18" /></>,
  stop: <><rect x="6" y="6" width="12" height="12" rx="2" /></>,
  spark: <><path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3Z" /></>,
};
export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  return <svg className="ico" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{P[name] ?? P.spark}</svg>;
}
