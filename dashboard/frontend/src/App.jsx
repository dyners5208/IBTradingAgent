import { useState } from 'react'
import './index.css'
import { useAutoRefresh } from './hooks/useAutoRefresh'
import Overview          from './components/Overview'
import PnlHistory        from './components/PnlHistory'
import ByStrategy        from './components/ByStrategy'
import CategoryAnalysis  from './components/CategoryAnalysis'
import OpenPositions     from './components/OpenPositions'
import ClosedTrades      from './components/ClosedTrades'
import WheelTracker      from './components/WheelTracker'
import Alerts            from './components/Alerts'
import SessionLog        from './components/SessionLog'
import Insights          from './components/Insights'
import Gems              from './components/Gems'
import { useMarketStatus } from './hooks/useMarketStatus'

const TABS = [
  { id: 'overview',  label: 'Overview',        Component: Overview         },
  { id: 'open',      label: 'Open Positions',  Component: OpenPositions    },
  { id: 'closed',    label: 'Closed Trades',   Component: ClosedTrades     },
  { id: 'pnl',       label: 'P&L History',     Component: PnlHistory       },
  { id: 'category',  label: 'By Category',     Component: CategoryAnalysis },
  { id: 'strategy',  label: 'By Strategy',     Component: ByStrategy       },
  { id: 'insights',  label: 'Insights',        Component: Insights         },
  { id: 'gems',      label: 'Gems',            Component: Gems             },
  { id: 'wheel',     label: 'Wheel Tracker',   Component: WheelTracker     },
  { id: 'alerts',    label: 'Alerts',          Component: Alerts           },
  { id: 'log',       label: 'Session Log',     Component: SessionLog       },
]

function MarketBadge({ label, open }) {
  return (
    <div className={`market-badge ${open ? 'open' : 'closed'}`}>
      <span className="dot" />
      {label} {open ? 'Open' : 'Closed'}
    </div>
  )
}

export default function App() {
  const [active, setActive] = useState('overview')
  const { usOpen, hkOpen, clock } = useMarketStatus()
  const current = TABS.find(t => t.id === active)
  const { data: accountData } = useAutoRefresh('/api/account', 60000)
  const isPaper = accountData?.is_paper !== false

  return (
    <>
      <div className="header">
        <div className="header-left">
          <div className="header-logo" />
          <h1>TRADE<span>/CTRL</span></h1>
          <span className={`mode-badge ${isPaper ? 'mode-paper' : 'mode-live'}`}>
            {isPaper ? 'PAPER' : 'LIVE'}
          </span>
        </div>
        <div className="header-right">
          <div className="market-badges">
            <MarketBadge label="US" open={usOpen} />
            <MarketBadge label="HK" open={hkOpen} />
          </div>
          <div className="header-clock">{clock}</div>
        </div>
      </div>

      <nav className="tabs">
        {TABS.map(t => (
          <button
            key={t.id}
            className={`tab-btn${active === t.id ? ' active' : ''}`}
            onClick={() => setActive(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <div className="tab-content">
        {current && <current.Component />}
      </div>
    </>
  )
}
