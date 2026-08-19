// THROWAWAY dev-only harness to visually verify the QuickKeys bar
// (spec-082 D) without needing a logged-in cockpit + backend. Not imported
// by anything, not built by `npm run build` (no <script> references it from
// index.html), and deleted before commit.
import ReactDOM from 'react-dom/client'
import './styles.css'
import { TerminalTab } from './tabs/TerminalTab'

ReactDOM.createRoot(document.getElementById('root')!).render(<TerminalTab isActive={true} />)
