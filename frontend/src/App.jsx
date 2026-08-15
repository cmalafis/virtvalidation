import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import AppLayout from "./layout/AppLayout";
import AgentActivityPage from "./pages/AgentActivityPage";
import BulkOperationsPage from "./pages/BulkOperationsPage";
import AuditLogPage from "./pages/AuditLogPage";
import DesignReviewDetailPage from "./pages/DesignReviewDetailPage";
import DesignReviewNewPage from "./pages/DesignReviewNewPage";
import DesignReviewsPage from "./pages/DesignReviewsPage";
import InventoryPage from "./pages/InventoryPage";
import OCPTargetsPage from "./pages/OCPTargetsPage";
import OCPTargetDetailPage from "./pages/OCPTargetDetailPage";
import OverviewPage from "./pages/OverviewPage";
import PlanDetailPage from "./pages/PlanDetailPage";
import PlansPage from "./pages/PlansPage";
import PlanWizardPage from "./pages/PlanWizardPage";
import ReportsPage from "./pages/ReportsPage";
import ReportViewPage from "./pages/ReportViewPage";
import ResourceMappingDetailPage from "./pages/ResourceMappingDetailPage";
import ResourceMappingsPage from "./pages/ResourceMappingsPage";
import RVToolsUploadPage from "./pages/RVToolsUploadPage";
import SettingsPage from "./pages/SettingsPage";
import VCenterSourcesPage from "./pages/VCenterSourcesPage";
import ValidationsPage from "./pages/ValidationsPage";
import VMDetailPage from "./pages/VMDetailPage";


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
          <Route path="vms/:id" element={<VMDetailPage />} />
          <Route path="design-reviews" element={<DesignReviewsPage />} />
          <Route path="rvtools/upload" element={<RVToolsUploadPage />} />
          <Route path="design-reviews/network/new" element={<DesignReviewNewPage kind="network" />} />
          <Route path="design-reviews/storage/new" element={<DesignReviewNewPage kind="storage" />} />
          <Route path="design-reviews/storage/:id" element={<DesignReviewDetailPage kind="storage" />} />
          <Route path="design-reviews/:id" element={<DesignReviewDetailPage kind="network" />} />

          {/* Migrate */}
          <Route path="plans" element={<PlansPage />} />
          <Route path="plans/new" element={<PlanWizardPage />} />
          <Route path="plans/:id" element={<PlanDetailPage />} />
          <Route path="operations" element={<BulkOperationsPage />} />

          {/* Verify */}
          <Route path="validations" element={<ValidationsPage />} />
          <Route path="reports" element={<ReportsPage />} />
          <Route path="reports/:type" element={<ReportViewPage />} />

          {/* Configure */}
          <Route path="sources/vcenters" element={<VCenterSourcesPage />} />
          <Route path="sources/targets" element={<OCPTargetsPage />} />
          <Route path="sources/targets/:id" element={<OCPTargetDetailPage />} />
          <Route path="mappings" element={<ResourceMappingsPage />} />
          <Route path="mappings/:id" element={<ResourceMappingDetailPage />} />

          {/* Administration */}
          <Route path="agent-activity" element={<AgentActivityPage />} />
          <Route path="audit" element={<AuditLogPage />} />
          <Route path="settings" element={<SettingsPage />} />

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
