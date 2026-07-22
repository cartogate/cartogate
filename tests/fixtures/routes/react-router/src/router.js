import { createBrowserRouter } from "react-router-dom";
import { Settings } from "./settings";

export const router = createBrowserRouter([
  {
    path: "/settings",
    element: Settings,
  },
]);

// A path-like property OUTSIDE any router context must NOT become a route:
export const notARoute = { path: "/etc/passwd" };
