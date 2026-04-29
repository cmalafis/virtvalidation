import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import Settings from "./components/Settings";
import VirtValidate from "./components/VirtValidate";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<VirtValidate />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
