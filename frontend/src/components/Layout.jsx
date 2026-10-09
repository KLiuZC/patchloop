import { useEffect } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { ArrowUpRight, CircleHelp, FolderGit2, Layers3, PanelLeft, Plus } from 'lucide-react'

export default function Layout() {
  const location = useLocation()
  const section = location.pathname === '/tasks/github' ? 'GitHub Issue' : location.pathname === '/tasks/new' ? '新建任务' : location.pathname === '/tasks' ? '任务列表' : '任务详情'
  const inTasks = !['/tasks/new', '/tasks/github'].includes(location.pathname)
  useEffect(() => {
    document.title = `${section} · patchloop`
    document.getElementById('page-title')?.focus()
  }, [location.pathname, section])

  return <div className="workbench">
    <a href="#main-content" className="skip-link">跳到主要内容</a>
    <aside className="sidebar">
      <Link to="/tasks" className="brand" aria-label="patchloop 任务列表">
        <img src="/favicon.svg" width="34" height="34" alt="" /><span>patchloop<small>修复工作台</small></span>
      </Link>
      <div className="nav-caption">工作空间</div>
      <nav aria-label="主要导航">
        <Link to="/tasks" className={`nav-item ${inTasks ? 'active' : ''}`} aria-current={inTasks ? 'location' : undefined}><Layers3 size={18} /><span>修复任务</span></Link>
        <NavLink to="/tasks/new" className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}><Plus size={18} /><span>新建任务</span></NavLink>
        <NavLink to="/tasks/github" className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}><FolderGit2 size={18} /><span>GitHub Issue</span></NavLink>
      </nav>
      <div className="sidebar-bottom">
        <details className="help-popover"><summary><CircleHelp size={17} /><span>如何开始</span></summary><p>提交修复要求后，由独立 worker 处理。选择一条任务，就能查看补丁与测试结果。</p><p>刷新或关闭网页不会终止后台任务。</p></details>
        <span className="local-label"><span className="small-dot" />本地工作空间<ArrowUpRight size={13} /></span>
      </div>
    </aside>
    <div className="workspace">
      <header className="topbar"><div className="breadcrumb"><PanelLeft size={16} /><span>工作空间</span><span className="crumb-divider">/</span><span>{section}</span></div></header>
      <main id="main-content" className="main-content"><Outlet /></main>
    </div>
  </div>
}
