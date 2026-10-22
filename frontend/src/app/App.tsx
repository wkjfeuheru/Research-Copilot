import { Route, Routes } from 'react-router-dom';

import { AppHeader } from '../components/AppHeader';
import { MemoryPage } from '../pages/MemoryPage';
import { NotFoundPage } from '../pages/NotFoundPage';
import { ResearchWorkspacePage } from '../pages/ResearchWorkspacePage';
import { SkillsPage } from '../pages/SkillsPage';

export function App() {
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <AppHeader />
      <main id="main-content" className="app-main">
        <Routes>
          <Route path="/" element={<ResearchWorkspacePage />} />
          <Route path="/memory" element={<MemoryPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </main>
    </div>
  );
}
