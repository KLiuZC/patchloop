import MarkdownBody from './MarkdownBody'

export default function IssueBody({ body, issueUrl, collapse = false }) {
  return <MarkdownBody body={body} baseUrl={issueUrl} collapse={collapse} label="Issue 正文" rawLabel="Issue 原文"
    heading="问题描述" collapseLabel="查看 Issue 正文" emptyText="此 Issue 没有正文。" />
}
