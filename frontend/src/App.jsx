import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Import from './pages/Import'
import Summary from './pages/Summary'
import QA from './pages/QA'
import IndexManager from './pages/IndexManager'
import Settings from './pages/Settings'
import { RunsProvider } from './lib/runsStore'

function App() {
  return (
    <RunsProvider>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/import" element={<Import />} />
          <Route path="/index" element={<IndexManager />} />
          <Route path="/summary" element={<Summary />} />
          <Route path="/qa" element={<QA />} />
          <Route path="/qa/:sessionId" element={<QA />} />
          <Route path="/settings" element={<Settings />} />
        </Route>
      </Routes>
    </RunsProvider>
  )
}

export default App
