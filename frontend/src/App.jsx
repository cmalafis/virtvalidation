import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import NetworkReviewDetail from "./components/NetworkReviewDetail";
import NetworkReviewNew from "./components/NetworkReviewNew";
import ReportView from "./components/ReportView";
import Settings from "./components/Settings";
import VCenterSources from "./components/VCenterSources";
import VirtValidate from "./components/VirtValidate";
import VMDetail from "./components/VMDetail";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<VirtValidate />} />
        <Route path="/vms/:id" element={<VMDetail />} />
        <Route path="/reports/:type" element={<ReportView />} />
        <Route path="/design-reviews/new" element={<NetworkReviewNew />} />
        <Route path="/design-reviews/:id" element={<NetworkReviewDetail />} />
        <Route path="/sources/vcenters" element={<VCenterSources />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
