import { createBrowserRouter } from "react-router-dom";

// Corpus-shaped (empty/apps/web-mockup, ai-meal-planner): the dominant
// real-world pattern is children-relative nesting — statically joinable
// within this ONE literal tree.
export const router = createBrowserRouter([
  {
    path: "/",
    children: [
      { index: true, element: null },
      { path: "dashboard", element: null },
      {
        path: "admin",
        children: [
          { path: "users", element: null },
          { path: "users/:userId", element: null },
          // absolute child (legal): stands alone regardless of parent
          { path: "/reports", element: null },
          // catch-all: not a navigable url pattern — skipped
          { path: "*", element: null },
        ],
      },
      // computed path: unresolvable — this child AND its relative children
      // are skipped, never guessed
      {
        path: dynamicSection(),
        children: [{ path: "sub", element: null }],
      },
    ],
  },
]);

function dynamicSection() {
  return "/computed";
}
