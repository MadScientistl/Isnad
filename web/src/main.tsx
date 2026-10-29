import { createRoot } from "react-dom/client";
import "maplibre-gl/dist/maplibre-gl.css";
import "./style.css";
import { MOCK, BASE } from "./api";

async function boot() {
  if (MOCK) (await import("./mock")).installMock(BASE);
  const { default: App } = await import("./App");
  createRoot(document.getElementById("root")!).render(<App />);
}
boot();
