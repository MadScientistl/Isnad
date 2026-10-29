import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@sdk": path.resolve(__dirname, "../sdk") } },
  server: { port: 5173, fs: { allow: [".."] } },
});
