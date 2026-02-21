import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";

const rootElement = document.getElementById("root");

if (!rootElement) {
  throw new Error("Root container missing in index.html");
}

const root = ReactDOM.createRoot(rootElement);

// IMPORTANT:
// ❌ No React.StrictMode
// ✔ Prevents double-render issues with charts & WebSockets

root.render(<App />);

