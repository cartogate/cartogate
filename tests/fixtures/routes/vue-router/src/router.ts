import { createRouter, createWebHistory } from "vue-router";
import Home from "./Home.vue";
import Product from "./Product.vue";
import Admin from "./Admin.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: Home },
    { path: "/products/:pid", component: Product },
    {
      path: "/admin",
      component: Admin,
      children: [
        // RELATIVE child path — resolution needs parent-join; skipped in v1:
        { path: "reports", component: Admin },
        // ABSOLUTE child path — legal Vue, extracted:
        { path: "/admin/audit", component: Admin },
      ],
    },
  ],
});

// A path-like property OUTSIDE createRouter must NOT become a route:
export const notARoute = { path: "/etc/shadow" };
