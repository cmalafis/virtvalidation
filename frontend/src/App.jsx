import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import AppLayout from "./layout/AppLayout";
import AuditLogPage from "./pages/AuditLogPage";
import DesignReviewsPage from "./pages/DesignReviewsPage";
import InventoryPage from "./pages/InventoryPage";
import OverviewPage from "./pages/OverviewPage";
import PlansPage from "./pages/PlansPage";
import ReportsPage from "./pages/ReportsPage";
import ValidationsPage from "./pages/ValidationsPage";

import AgentActivity from "./components/AgentActivity";
import BulkOperations from "./components/BulkOperations";
import NetworkReviewDetail from "./components/NetworkReviewDetail";
import NetworkReviewNew from "./components/NetworkReviewNew";
import OCPTargetDetail from "./components/OCPTargetDetail";
import OCPTargets from "./components/OCPTargets";
import PlanView from "./components/PlanView";
import PlanWizard from "./components/PlanWizard";
import ReportView from "./components/ReportView";
import ResourceMappings, { ResourceMappingDetail } from "./components/ResourceMappings";
import RVToolsUpload from "./components/RVToolsUpload";
import Settings from "./components/Settings";
import StorageReviewDetail from "./components/StorageReviewDetail";
import StorageReviewNew from "./components/StorageReviewNew";
import VCenterSources from "./components/VCenterSources";
import VMDetail from "./components/VMDetail";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Every page is a child of the layout route, so the masthead and
            sidebar are present everywhere. Before this, navigation existed
            only on "/" and each page hand-rolled its own back-link. */}
        <Route element={<AppLayout />}>
          <Route index element={<OverviewPage />} />

          {/* Discover */}
          <Route path="inventory" element={<InventoryPage />} />
          <Route path="vms/:id" element={<VMDetail />} />
          <Route path="design-reviews" element={<DesignReviewsPage />} />
          <Route path="rvtools/upload" element={<RVToolsUpload />} />
          <Route path="design-reviews/network/new" element={<NetworkReviewNew />} />
          <Route path="design-reviews/storage/new" element={<StorageReviewNew />} />
          <Route path="design-reviews/storage/:id" element={<StorageReviewDetail />} />
          <Route path="design-reviews/:id" element={<NetworkReviewDetail />} />

          {/* Migrate */}
          <Route path="plans" element={<PlansPage />} />
          <Route path="plans/new" element={<PlanWizard />} />
          <Route path="plans/:id" element={<PlanView />} />
          <Route path="operations" element={<BulkOperations />} />

          {/* Verify */}
          <Route path="validations" element={<ValidationsPage />} />
          <Route path="reports" element={<ReportsPage />} />
          <Route path="reports/:type" element={<ReportView />} />

          {/* Configure */}
          <Route path="sources/vcenters" element={<VCenterSources />} />
          <Route path="sources/targets" element={<OCPTargets />} />
          <Route path="sources/targets/:id" element={<OCPTargetDetail />} />
          <Route path="mappings" element={<ResourceMappings />} />
          <Route path="mappings/:id" element={<ResourceMappingDetail />} />

          {/* Administration */}
          <Route path="agent-activity" element={<AgentActivity />} />
          <Route path="audit" element={<AuditLogPage />} />
          <Route path="settings" element={<Settings />} />

          {/* Back-compat: paths that shipped earlier and may be
              bookmarked. Kept as redirects rather than duplicate routes
              so there is exactly one canonical URL per screen. */}
          <Route
            path="design-reviews/new"
            element={<Navigate to="/design-reviews/network/new" replace />}
          />
          <Route path="capture-baselines" element={<Navigate to="/operations" replace />} />
          <Route path="validate-batch" element={<Navigate to="/operations" replace />} />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
