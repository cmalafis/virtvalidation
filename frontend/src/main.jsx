import React from 'react'
import ReactDOM from 'react-dom/client'

import './index.css'
import App from './App'
import { initTheme } from './theme'

// Apply the stored theme before the first paint so there's no flash of
// the wrong theme on load.
initTheme()

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
