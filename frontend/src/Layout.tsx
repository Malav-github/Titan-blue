import { NavLink, Outlet } from 'react-router-dom'

const NAV = [
  { to: '/', label: 'Home', end: true },
  { to: '/runs', label: 'Runs' },
  { to: '/connectors', label: 'Data sources' },
  { to: '/evidence', label: 'Evidence' },
  { to: '/health', label: 'Health' },
]

export function Layout() {
  return (
    <div className="min-h-screen md:flex">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:rounded focus:bg-accent focus:px-3 focus:py-1 focus:text-accent-fg">
        Skip to content
      </a>
      <nav aria-label="Main" className="border-b border-line bg-surface p-3 md:min-h-screen md:w-52 md:shrink-0 md:border-b-0 md:border-r">
        <p className="mb-3 px-2 text-lg font-semibold tracking-tight">Titan</p>
        <ul className="flex flex-wrap gap-1 md:flex-col">
          {NAV.map((n) => (
            <li key={n.to}>
              <NavLink
                to={n.to}
                end={n.end}
                className={({ isActive }) =>
                  `block rounded-md px-2 py-1.5 text-sm ${isActive ? 'bg-accent text-accent-fg' : 'hover:bg-surface-2'}`
                }
              >
                {n.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <main id="main" className="min-w-0 flex-1 p-4 md:p-6">
        <div className="mx-auto max-w-5xl">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
