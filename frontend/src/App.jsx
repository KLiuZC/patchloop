import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import TaskList from './pages/TaskList'
import NewTask from './pages/NewTask'
import TaskDetail from './pages/TaskDetail'

export default function App() {
  return <Routes><Route element={<Layout />}>
    <Route path="/" element={<Navigate to="/tasks" replace />} />
    <Route path="/tasks" element={<TaskList />} />
    <Route path="/tasks/new" element={<NewTask />} />
    <Route path="/tasks/:taskId" element={<TaskDetail />} />
    <Route path="*" element={<Navigate to="/tasks" replace />} />
  </Route></Routes>
}
