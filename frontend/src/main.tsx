import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/manrope";
import { queryClient } from "./api/query-client";
import { router } from "./app/router";
import { initializeI18n } from "./i18n";
import "./styles/index.css";

await initializeI18n();

const root = document.getElementById("root");
if (root === null) {
  throw new Error("Comet frontend root is missing");
}

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
