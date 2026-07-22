import { Route, Routes } from "react-router-dom";

const SHOW_PROMO = true;

// Nested <Route> JSX with relative children — same static-join semantics as
// the object-literal form.
export default function JsxApp() {
  return (
    <Routes>
      <Route path="/shop">
        <Route path="cart" element={null} />
        <Route path="checkout/:step" element={null} />
        {SHOW_PROMO && <Route path="promo" element={null} />}
      </Route>
    </Routes>
  );
}
