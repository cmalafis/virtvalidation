import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import NetworkReviewDetail from "./components/NetworkReviewDetail";
import NetworkReviewNew from "./components/NetworkReviewNew";
import PlanView from "./components/PlanView";
import PlanWizard from "./components/PlanWizard";
import ReportView from "./components/ReportView";
import Settings from "./components/Settings";
import StorageReviewDetail from "./components/StorageReviewDetail";
import StorageReviewNew from "./components/StorageReviewNew";
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
        {/* Network design review — unprefixed path kept for back-compat
            with existing bookmarks. Storage lives at the prefixed path. */}
        <Route path="/design-reviews/new" element={<NetworkReviewNew />} />
        <Route path="/design-reviews/network/new" element={<NetworkReviewNew />} />
        <Route path="/design-reviews/storage/new" element={<StorageReviewNew />} />
        <Route path="/design-reviews/storage/:id" element={<StorageReviewDetail />} />
        <Route path="/design-reviews/:id" element={<NetworkReviewDetail />} />
        <Route path="/plans/new" element={<PlanWizard />} />
        <Route path="/plans/:id" element={<PlanView />} />
        <Route path="/sources/vcenters" element={<VCenterSources />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
