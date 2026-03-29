import React from 'react';
import EKSCostOptimizer from './EKSCostOptimizerDashboard';

// In production this is injected by the Helm chart as window.APP_CONFIG.
// In local dev the CRA proxy (package.json "proxy") forwards /api/* to :8080.
const API_URL = window.APP_CONFIG?.apiUrl ?? '';

export default function App() {
  return <EKSCostOptimizer apiUrl={API_URL} />;
}
