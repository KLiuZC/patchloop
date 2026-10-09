import SyntaxHighlighter from 'react-syntax-highlighter/dist/esm/prism-light'
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python'
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash'

SyntaxHighlighter.registerLanguage('python', python)
SyntaxHighlighter.registerLanguage('bash', bash)

export default function VerificationHighlight({ code, language, label }) {
  return <SyntaxHighlighter language={language} useInlineStyles={false} showLineNumbers wrapLongLines
    lineProps={{ style: { display: 'block', position: 'relative', paddingLeft: '4em', minHeight: '1.8em' } }}
    lineNumberStyle={{ position: 'absolute', left: 0, width: '3em', paddingRight: '1em', userSelect: 'none' }}
    customStyle={{ margin: 0, padding: '1rem 1rem 1rem 0', background: 'transparent' }}
    tabIndex={0} aria-label={label} className="verification-code-content">
    {code}
  </SyntaxHighlighter>
}
