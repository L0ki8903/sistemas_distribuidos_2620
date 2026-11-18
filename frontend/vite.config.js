import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// En desarrollo (`npm run dev`) las llamadas a /api se envían al gateway NGINX del clúster
// (o a un nodo suelto con VITE_API_PROXY=http://localhost:8000). En producción NGINX sirve
// el build y las rutas relativas /api/... llegan al mismo origen: nunca hay URLs absolutas.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY || "http://localhost:8080",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
