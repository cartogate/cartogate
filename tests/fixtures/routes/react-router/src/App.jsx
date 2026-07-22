import { Link, Route, Routes } from "react-router-dom";

const section = computeSection();

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/users/:userId" element={<User />} />
      <Route path={section} element={<Dynamic />} />
    </Routes>
  );
}

function Home() {
  return <Link to="/users/42">A user</Link>;
}

function User() {
  return <h1>User</h1>;
}

function Dynamic() {
  return <h1>Dynamic</h1>;
}

function computeSection() {
  return "/computed";
}
