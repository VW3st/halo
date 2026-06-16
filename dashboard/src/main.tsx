import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'
import { startPolling } from './lib/api'

// Begin polling the Flask backend immediately (events, agent/tool state,
// system metrics). The store updates; components react.
startPolling()

// NOTE: no React.StrictMode — its double-invoke remounts the WebGL context
// in dev and can cause the R3F canvas to flicker / leak GL contexts.
ReactDOM.createRoot(document.getElementById('root')!).render(<App />)
