import Header from './components/Header'
import { ListeningPanel, TimeDomainPanel, FftPanel } from './components/ListeningPanel'
import CenterStage from './components/CenterStage'
import ToolRouter from './components/ToolRouter'
import AgentCards from './components/AgentCards'
import SystemMetrics from './components/SystemMetrics'
import StateTimeline from './components/StateTimeline'
import OutputPanel from './components/OutputPanel'
import Footer from './components/Footer'

export default function App() {
  return (
    // Mobile: a single scrollable column (flex-col). lg+: the fixed desktop
    // grid. Tailwind lg: arbitrary values carry the exact desktop template so
    // the layout is identical on desktop and usable on a phone.
    <div className="relative z-[1] flex flex-col gap-2 p-2.5 lg:grid lg:h-screen lg:grid-rows-[54px_minmax(0,1fr)_clamp(208px,23vh,252px)_26px]">
      <Header />

      {/* main row: listening | core | tools/agents */}
      <main className="flex flex-col gap-2 lg:grid lg:min-h-0 lg:grid-cols-[312px_minmax(0,1fr)_344px]">
        <div className="flex flex-col gap-2 lg:grid lg:min-h-0 lg:grid-rows-[1.25fr_1fr_1fr]">
          <ListeningPanel />
          <TimeDomainPanel />
          <FftPanel />
        </div>
        <CenterStage />
        <div className="flex flex-col gap-2 lg:grid lg:min-h-0 lg:grid-rows-[1.55fr_1fr]">
          <ToolRouter />
          <AgentCards />
        </div>
      </main>

      {/* bottom row: metrics | timeline | output */}
      <section className="flex flex-col gap-2 lg:grid lg:min-h-0 lg:grid-cols-[312px_minmax(0,1fr)_344px]">
        <SystemMetrics />
        <StateTimeline />
        <OutputPanel />
      </section>

      <Footer />
    </div>
  )
}
