import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './index.css'
import { Layout } from './Layout'
import { Home } from './pages/Home'
import { AppForm } from './pages/AppForm'
import { Runs } from './pages/Runs'
import { RunPage } from './pages/RunPage'
import { Connectors } from './pages/Connectors'
import { RequestPage } from './pages/RequestPage'
import { Evidence } from './pages/Evidence'
import { ClaimPage } from './pages/ClaimPage'
import { HealthPage } from './pages/HealthPage'
import { PageHeader } from './ui'

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } })

function NotFound() {
  return <PageHeader title="Page not found" subtitle="That address does not exist in Titan." />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Home />} />
            <Route path="apps/:id" element={<AppForm />} />
            <Route path="runs" element={<Runs />} />
            <Route path="runs/:id" element={<RunPage />} />
            <Route path="connectors" element={<Connectors />} />
            <Route path="connectors/requests/:id" element={<RequestPage />} />
            <Route path="evidence" element={<Evidence />} />
            <Route path="evidence/:id" element={<ClaimPage />} />
            <Route path="health" element={<HealthPage />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
