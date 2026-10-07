import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { MotionConfig } from 'motion/react'
import App from './App'
import { createQueryClient } from './lib/query-client'
import './styles.css'

const queryClient = createQueryClient()

createRoot(document.getElementById('root')).render(
  <StrictMode><QueryClientProvider client={queryClient}><MotionConfig reducedMotion="user"><BrowserRouter><App /></BrowserRouter></MotionConfig></QueryClientProvider></StrictMode>,
)
