import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Self-hosted through npm rather than a font CDN: the board is served as
// static files from one origin, and a webfont fetched from a second one is
// the only request on the page that could hang.
import "@fontsource-variable/archivo";
import "@fontsource/archivo-narrow/400.css";
import "@fontsource/archivo-narrow/600.css";
import "@fontsource/archivo-narrow/700.css";

import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/components.css";
import "./styles/chart.css";

import { App } from "./app";

const root = document.getElementById("root");
if (!root) throw new Error("no #root in index.html");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
